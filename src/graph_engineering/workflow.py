"""The workflow: a fixed sequence of steps around two agents. No model decides a transition.

    new ──intake──► proposed ──accept──► accepted ──round──► ready ──publish──► awaiting_merge ──approve──► merging ──► done

A development round is: coding agent → gates → review agent. A failed gate or a review asking for changes
becomes the next round's feedback, until the round budget is spent.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import time
from typing import Callable

from . import gates, prompts
from .agent import Agent, Outcome
from .llm import Registry
from .publish import GitHub, Local, PublishError
from .repos import Repo, Repos
from .sandbox import Sandbox, clip, git, prepare_workspace
from .store import Conflict, Store
from .tools import READ_ONLY, READ_WRITE, Workspace

DIFF_CHARS = 60000
FEEDBACK_CHARS = 12000
MERGE_POLL = 60
MERGE_PATIENCE = 6 * 3600


@dataclass
class Context:
    home: Path
    store: Store
    repos: Repos
    registry: Registry
    owner: str = "worker"
    renew: Callable[[], None] = lambda: None

    def ticket_dir(self, ticket: str) -> Path:
        return self.home / "work" / ticket


def advance(ctx: Context, ticket: dict) -> dict:
    """Run the one step a leased ticket is due for."""
    step = {"new": intake, "accepted": develop, "ready": publish, "merging": merge}[ticket["state"]]
    return step(ctx, ticket)


def _move(ctx: Context, ticket: dict, to: str, kind: str, event: dict | None = None, **fields) -> dict:
    return ctx.store.transition(ticket["id"], to, ctx.owner, kind, event, expect=(ticket["state"],), owner=ctx.owner, **fields)


def _agent(ctx: Context, ticket: dict, role: str, workspace: Workspace, tools, finish, system, directory: Path, round_: int,
           exclude: set = frozenset(), **options) -> tuple[Agent, str, object]:
    model = ctx.registry.for_role(role, exclude)
    directory.mkdir(parents=True, exist_ok=True)
    run = ctx.store.run_start(ticket["id"], role, model.name, round_, directory)

    def on_step(event: dict) -> None:
        ctx.renew()
        ctx.store.event(ticket["id"], "agent", f"{role}:{model.name}", {"run": run, **event})

    cancelled = lambda: (ctx.store.get(ticket["id"]) or {}).get("state") == "cancelled"
    agent = Agent(model, workspace, tools, finish, system, transcript=directory / "transcript.jsonl", on_step=on_step,
                  cancelled=cancelled, **options)
    return agent, run, model


def _explorer(ctx: Context, ticket: dict, workspace: Workspace, directory: Path, round_: int) -> Callable[[str], str]:
    """A sub-agent in a fresh read-only context; only its answer reaches the caller's context."""
    count = [0]

    def explore(question: str) -> str:
        count[0] += 1
        agent, run, _ = _agent(ctx, ticket, "explore" if "explore" in ctx.registry.roles else "coding", workspace, READ_ONLY,
                               prompts.REPORT, prompts.EXPLORE, directory / f"explore-{count[0]}", round_, max_steps=20,
                               working_tokens=20000)
        outcome = agent.run(f"Question: {question}\n\n<project>\n{prompts.project_brief(workspace.root)}\n</project>")
        ctx.store.run_end(run, outcome.status, outcome.usage)
        if outcome.status == "done":
            return clip(outcome.result["answer"], 4000)
        return f"The helper could not answer ({outcome.status}). Investigate yourself."

    return explore


# ── intake ──────────────────────────────────────────────────────────────────────────────────────────────

def intake(ctx: Context, ticket: dict) -> dict:
    repo = ctx.repos.get(ticket["repo"])
    directory = ctx.ticket_dir(ticket["id"]) / "intake"
    if directory.exists():
        shutil.rmtree(directory)
    prepare_workspace(Path(repo.path), repo.base, directory / "work")
    agent, run, model = _agent(ctx, ticket, "intake", Workspace(directory / "work"), READ_ONLY, prompts.SUBMIT_TICKET,
                               prompts.INTAKE, directory, 0, max_steps=40)
    outcome = agent.run(prompts.intake_task(ticket["request"], repo.name, prompts.project_brief(directory / "work")))
    ctx.store.run_end(run, outcome.status, outcome.usage)
    shutil.rmtree(directory / "work", ignore_errors=True)
    if outcome.status == "cancelled":
        return ctx.store.get(ticket["id"])
    if outcome.status != "done":
        return _move(ctx, ticket, "failed", "intake_failed", {"outcome": outcome.asdict()},
                     error=f"intake agent {outcome.status}" + (f": {outcome.error}" if outcome.error else ""),
                     data={**ticket["data"], "failed_at": "intake"})
    result = outcome.result
    spec = {k: result.get(k) for k in ("title", "kind", "problem", "acceptance_criteria", "relevant_files")}
    if result["verdict"] == "needs_info":
        return _move(ctx, ticket, "needs_info", "questions", {"questions": result.get("questions") or [], "reason": result["reason"]},
                     spec=spec, error=None)
    if result["verdict"] == "not_actionable" or not result.get("acceptance_criteria"):
        return _move(ctx, ticket, "rejected", "not_actionable", {"reason": result["reason"]}, spec=spec, error=None)
    ticket = _move(ctx, ticket, "proposed", "proposed", {"model": model.name, "reason": result["reason"]}, spec=spec, error=None)
    if repo.auto_accept:
        ticket = accept(ctx.store, ctx.repos, ticket["id"], "policy:auto_accept")
    return ticket


def base_ref(repo: Repo, data: dict) -> str:
    """What development starts from: a pinned commit if one is given, else the freshly fetched GitHub base."""
    if data.get("base_ref"):
        return data["base_ref"]
    if repo.slug:
        git(Path(repo.path), "fetch", "-q", "origin", f"+refs/heads/{repo.base}:refs/remotes/origin/{repo.base}")
        return f"origin/{repo.base}"
    return repo.base


# ── development rounds ─────────────────────────────────────────────────────────────────────────────────

def develop(ctx: Context, ticket: dict) -> dict:
    repo = ctx.repos.get(ticket["repo"])
    round_ = ticket["round"] + 1
    data = dict(ticket["data"])
    if round_ > ticket["max_rounds"]:
        return _move(ctx, ticket, "failed", "rounds_spent", error=f"no accepted result after {ticket['max_rounds']} rounds",
                     data={**data, "failed_at": "develop"})
    root = ctx.ticket_dir(ticket["id"])
    work, directory = root / "work", root / f"round-{round_}"
    if directory.exists():
        shutil.rmtree(directory)  # an interrupted attempt of this round starts again
    directory.mkdir(parents=True)
    sandbox = Sandbox(root, work, repo.sandbox_env(), trusted=repo.trusted)
    if not (work / ".git").exists() or not data.get("setup_done"):
        # A crash between cloning and the end of setup leaves a half-prepared workspace: always start it again.
        if work.exists():
            shutil.rmtree(work)
        data["base"] = prepare_workspace(Path(repo.path), base_ref(repo, data), work)
        for command in repo.setup:
            code, out, seconds = sandbox.run(repo.expand(command), timeout=1800, network=True)
            ctx.store.event(ticket["id"], "setup", ctx.owner, {"command": command, "exit": code, "seconds": round(seconds),
                                                                "output": clip(out, 2000)})
            if code:
                shutil.rmtree(work, ignore_errors=True)
                return _move(ctx, ticket, "failed", "setup_failed", {"command": command}, round=round_,
                             error=f"setup command failed: {command}", data={**data, "failed_at": "develop"})
        data.update(setup_done=True, dependencies=gates.installed(work))
        ctx.store.merge_data(ticket["id"], base=data["base"], setup_done=True, dependencies=data["dependencies"])
    spec = ticket["spec"]

    brief = prompts.project_brief(work)
    explorer = _explorer(ctx, ticket, Workspace(work), directory / "coding", round_)
    agent, run, coder = _agent(ctx, ticket, "coding", Workspace(work, sandbox, repo.run_timeout), READ_WRITE, prompts.FINISH,
                               prompts.CODING, directory / "coding", round_, max_steps=repo.coding_steps, nudge_after=15,
                               notes=root / "notes.md", explorer=explorer, evidence=True)
    outcome = agent.run(prompts.coding_task(spec, {"test_command": repo.test_command, "build_command": repo.build_command},
                                            data.get("feedback"), brief))
    ctx.store.run_end(run, outcome.status, outcome.usage)
    record = {"round": round_, "coding": {"model": coder.name, **outcome.asdict()}}
    if outcome.status == "cancelled":
        return ctx.store.get(ticket["id"])
    if outcome.status == "error":
        # The model server failed, not the work: the round does not count.
        ctx.store.merge_data(ticket["id"], last_error=outcome.error)
        raise RuntimeError(outcome.error)

    ctx.renew()
    report = gates.run_gates(repo, work, data["base"], sandbox, directory, data.get("dependencies"),
                             allow_test_config=bool(spec.get("allow_test_config")))
    (directory / "gates.json").write_text(json.dumps(report, indent=1))
    record["gates"] = [{"name": c["name"], "ok": c["ok"]} for c in report["checks"]]
    ctx.store.event(ticket["id"], "gates", "harness", {"round": round_, "passed": report["passed"], "checks": record["gates"]})
    summary = (outcome.result or {}).get("summary") or f"(the coding agent stopped without a summary: {outcome.status})"

    if report["passed"]:
        ctx.renew()
        reviewer_ws = Workspace(work)
        agent, run, reviewer = _agent(ctx, ticket, "review", reviewer_ws, READ_ONLY, prompts.SUBMIT_REVIEW, prompts.REVIEW,
                                      directory / "review", round_, exclude={coder.name}
                                      if len(ctx.registry.roles.get("review", [])) > 1 else frozenset(), max_steps=30)
        diff_text = gates.diff(work, data["base"], report["commit"])
        review = agent.run(prompts.review_task(spec, clip(diff_text, DIFF_CHARS), gates.summary(report), summary, brief))
        ctx.store.run_end(run, review.status, review.usage)
        if review.status == "cancelled":
            return ctx.store.get(ticket["id"])
        if review.status == "error":
            raise RuntimeError(review.error)
        verdict = (review.result or {}).get("verdict") if review.status == "done" else None
        record["review"] = {"model": reviewer.name, "status": review.status, **(review.result or {})}
        if verdict == "approve":
            # The published commit has exactly the tree the gates tested; a later round stacks on the previous one.
            message = f"{spec['title']}\n\n{neutral(summary)}\n\nTicket: {ticket['id']}"
            head = git(work, "commit-tree", report["tree"], "-p", data.get("head") or data["base"], "-m", message).strip()
            git(work, "reset", "-q", "--soft", head)
            data.update(head=head, summary=summary, round_dir=str(directory), gates=record["gates"], review=record["review"], publish=repo.auto_publish,
                        rounds=data.get("rounds", []) + [record], feedback=None)
            return _move(ctx, ticket, "ready", "ready", {"round": round_, "head": head}, round=round_, data=data, error=None)
        feedback = "The reviewer requested changes:\n" + "\n".join(
            f"- {f.get('file')}: {f.get('problem')} Fix: {f.get('fix')}" for f in (review.result or {}).get("findings", [])) \
            if verdict == "changes" else f"The review did not complete ({review.status}); make sure the change is clear and complete."
    else:
        feedback = gates.failures(report)
    data.update(feedback=clip(feedback, FEEDBACK_CHARS), rounds=data.get("rounds", []) + [record])
    if round_ >= ticket["max_rounds"]:
        return _move(ctx, ticket, "failed", "rounds_spent", {"round": round_}, round=round_,
                     error=f"no accepted result after {round_} rounds", data={**data, "failed_at": "develop"})
    return _move(ctx, ticket, "accepted", "round_rejected", {"round": round_}, round=round_, data=data, error=None)


# ── publication and merge ──────────────────────────────────────────────────────────────────────────────

def backend(repo: Repo):
    return GitHub(repo.slug, repo.base) if repo.slug else Local(Path(repo.path), repo.base)


def branch_name(repo: Repo, ticket: dict, issue: int | None) -> str:
    slug = "-".join(re.findall(r"[a-z0-9]+", ticket["spec"].get("title", "").lower()))[:40].strip("-") or "change"
    if issue is None:
        return f"ge/{ticket['id'].lower()}-{slug}"
    return repo.branch_template.format(kind=ticket["spec"].get("kind") or "feat", issue=issue, slug=slug, ticket=ticket["id"].lower())


def neutral(text: str) -> str:
    """Model-written prose for GitHub: no closing keywords that would close other issues, no @mentions."""
    text = re.sub(r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)(\s+)#(\d+)", r"\1\2issue \3", text, flags=re.I)
    return re.sub(r"@(?=[A-Za-z0-9-])", "@\u200b", text)


def pr_body(ticket: dict, issue: int | None) -> str:
    data, spec = ticket["data"], ticket["spec"]
    review = data.get("review") or {}
    lines = [f"Closes #{issue}" if issue else f"Ticket {ticket['id']}", "", "## Change", neutral(data.get("summary") or spec["title"]), "",
             "Acceptance criteria:", prompts.criteria_text(spec), "", "## Validation",
             f"Measured by the graph_engineering harness in a sandbox on {data['head']} (base {data['base']}):"]
    lines += [f"- {'passed' if g['ok'] else 'FAILED'}: {g['name']}" for g in data.get("gates", [])]
    lines += ["", "## Review", f"Model review ({review.get('model', 'unknown')}): {review.get('verdict', 'none')}.",
              neutral(str(review.get("summary", "")).strip()), "",
              f"Implemented by a local model in {len(data.get('rounds', []))} round(s). Merging requires a person's approval."]
    return "\n".join(lines)


def publish(ctx: Context, ticket: dict) -> dict:
    repo = ctx.repos.get(ticket["repo"])
    data, spec = dict(ticket["data"]), ticket["spec"]
    work = ctx.ticket_dir(ticket["id"]) / "work"
    target = backend(repo)
    try:
        issue = data.get("issue")
        if isinstance(target, GitHub) and repo.issue_required and not issue:
            issue = target.ensure_issue(ticket["id"], spec["title"], f"{spec['problem']}\n\nAcceptance criteria:\n"
                                        f"{prompts.criteria_text(spec)}")
            data = ctx.store.merge_data(ticket["id"], issue=issue)
        branch = data.get("branch") or branch_name(repo, ticket, issue)
        pr = target.ensure_pr(ticket["id"], work, branch, data["head"], spec["title"], pr_body({**ticket, "data": data}, issue))
        target.record_status(data["head"], True, "tests, red/green and build passed in the harness; model review approved")
    except (PublishError, RuntimeError) as exc:
        ctx.store.event(ticket["id"], "publish_failed", ctx.owner, {"error": str(exc)})
        ctx.store.release(ticket["id"], ctx.owner, error=f"publication failed: {exc}",
                          data=json.dumps({**data, "publish": False}))
        return ctx.store.get(ticket["id"])
    data.update(pr=pr, branch=pr["branch"])
    return _move(ctx, ticket, "awaiting_merge", "published", {"pr": pr}, data=data, error=None)


def merge(ctx: Context, ticket: dict) -> dict:
    repo = ctx.repos.get(ticket["repo"])
    data = dict(ticket["data"])
    target = backend(repo)
    pr = data["pr"]
    result = target.merge(pr["number"] if isinstance(target, GitHub) else pr["branch"], data["approved_head"])
    if result.get("merged"):
        data["merge_commit"] = result["merge_commit"]
        return _move(ctx, ticket, "done", "merged", {"merge_commit": result["merge_commit"]}, data=data, error=None)
    reasons = "; ".join(result.get("reasons", []))
    waited = time.time() - data.get("approved_at", time.time())
    if result.get("pending") and waited < MERGE_PATIENCE:
        return _move(ctx, ticket, "merging", "merge_waiting", {"reasons": result.get("reasons")},
                     not_before=time.time() + MERGE_POLL, error=f"waiting: {reasons}")
    return _move(ctx, ticket, "awaiting_merge", "merge_blocked", {"reasons": result.get("reasons")}, error=f"merge blocked: {reasons}")


# ── a person's actions ─────────────────────────────────────────────────────────────────────────────────

def accept(store: Store, repos: Repos, ticket_id: str, actor: str, spec: dict | None = None) -> dict:
    ticket = store.get(ticket_id)
    spec = {**ticket["spec"], **(spec or {})}
    if not spec.get("title") or not spec.get("acceptance_criteria"):
        raise Conflict("a ticket needs a title and acceptance criteria before development")
    return store.transition(ticket_id, "accepted", actor, "accepted", expect=("proposed",), spec=spec)


def reject(store: Store, ticket_id: str, actor: str, reason: str) -> dict:
    return store.transition(ticket_id, "rejected", actor, "rejected", {"reason": reason}, expect=("proposed", "needs_info"))


def answer(store: Store, ticket_id: str, actor: str, text: str) -> dict:
    ticket = store.get(ticket_id)
    request = f"{ticket['request']}\n\nClarification from the person: {text.strip()}"
    store.event(ticket_id, "answered", actor, {"text": text})
    with store.connect() as db:
        db.execute("UPDATE tickets SET request = ? WHERE id = ? AND state = 'needs_info'", (request, ticket_id))
    return store.transition(ticket_id, "new", actor, "reopened", expect=("needs_info",))


def approve_publish(store: Store, ticket_id: str, actor: str) -> dict:
    ticket = store.get(ticket_id)
    if ticket["state"] != "ready":
        raise Conflict(f"{ticket_id} is {ticket['state']}, not ready")
    data = store.merge_data(ticket_id, publish=True)
    store.event(ticket_id, "publish_approved", actor, {"head": data.get("head")})
    with store.connect() as db:
        db.execute("UPDATE tickets SET error = NULL, not_before = 0 WHERE id = ?", (ticket_id,))
    return store.get(ticket_id)


def approve_merge(store: Store, ticket_id: str, actor: str, head: str) -> dict:
    """The person approves one exact commit; a later push needs a new approval."""
    ticket = store.get(ticket_id)
    pr = ticket["data"].get("pr") or {}
    if ticket["state"] != "awaiting_merge" or head != pr.get("head"):
        raise Conflict("approval must name the PR's current head commit")
    data = {**ticket["data"], "approved_head": head, "approved_by": actor, "approved_at": time.time()}
    return store.transition(ticket_id, "merging", actor, "merge_approved", {"head": head}, expect=("awaiting_merge",),
                            data=data, not_before=0, error=None)


def request_changes(store: Store, ticket_id: str, actor: str, comment: str) -> dict:
    """Send a ready or published change back for another round, with the person's comment as feedback."""
    ticket = store.get(ticket_id)
    data = {**ticket["data"], "feedback": f"A person reviewed the change and asked for changes:\n{comment.strip()}",
            "approved_head": None, "publish": None}
    return store.transition(ticket_id, "accepted", actor, "changes_requested", {"comment": comment},
                            expect=("ready", "awaiting_merge"), data=data, max_rounds=max(ticket["max_rounds"], ticket["round"] + 1))


def retry(store: Store, ticket_id: str, actor: str) -> dict:
    ticket = store.get(ticket_id)
    if ticket["data"].get("failed_at") == "intake" or not ticket["spec"].get("acceptance_criteria"):
        return store.transition(ticket_id, "new", actor, "retried", expect=("failed", "rejected"), error=None)
    return store.transition(ticket_id, "accepted", actor, "retried", {"extra_round": True}, expect=("failed",),
                            max_rounds=max(ticket["max_rounds"], ticket["round"] + 1), error=None, not_before=0)


def cancel(store: Store, ticket_id: str, actor: str, reason: str = "") -> dict:
    return store.transition(ticket_id, "cancelled", actor, "cancelled", {"reason": reason})
