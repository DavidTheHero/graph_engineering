"""The workflow: a fixed sequence of steps around two agents. No model decides a transition.

    new ──intake──► proposed ──accept──► accepted ──round──► ready ──publish──► awaiting_merge ──approve──► merging ──► done

A development round is: coding agent → gates → review agent. A failed gate or a review asking for changes
becomes the next round's feedback, until the round budget is spent.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
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
           exclude: set = frozenset(), prompt_chars: int = 0, **options) -> tuple[Agent, str, object]:
    model = ctx.registry.for_role(role, exclude, prompt_chars)
    directory.mkdir(parents=True, exist_ok=True)
    run = ctx.store.run_start(ticket["id"], role, model.name, round_, directory)

    def on_step(event: dict) -> None:
        ctx.renew()
        ctx.store.event(ticket["id"], "agent", f"{role}:{model.name}", {"run": run, **event})

    cancelled = lambda: (ctx.store.get(ticket["id"]) or {}).get("state") == "cancelled"
    agent = Agent(model, workspace, tools, finish, system, transcript=directory / "transcript.jsonl", on_step=on_step,
                  cancelled=cancelled, **options)
    return agent, run, model


CODING_THINKING = 2048
failure_lines = gates.failure_lines


def run_timeout(repo, suite_seconds: int | None) -> int:
    """An agent's command limit: a few times the base suite's time, so a hang shows in a minute, not five."""
    if suite_seconds is None:
        return repo.run_timeout
    return min(repo.run_timeout, max(60, 4 * suite_seconds + 30))


def _explorer(ctx: Context, ticket: dict, workspace: Workspace, directory: Path, round_: int) -> Callable[[str], str]:
    """A sub-agent in a fresh read-only context; only its answer reaches the caller's context."""
    count = [0]

    def explore(question: str) -> str:
        count[0] += 1
        brief = prompts.project_brief(workspace.root, read_memory(ctx, ticket["repo"]), question, slim=True)
        task = f"Question: {question}\n\n<project>\n{brief}\n</project>"
        agent, run, _ = _agent(ctx, ticket, "explore" if "explore" in ctx.registry.roles else "coding", workspace, READ_ONLY,
                               prompts.REPORT, prompts.EXPLORE, directory / f"explore-{count[0]}", round_, max_steps=20,
                               working_tokens=20000, prompt_chars=len(task) + len(prompts.EXPLORE), thinking_budget=2048)
        outcome = agent.run(task)
        ctx.store.run_end(run, outcome.status, outcome.usage)
        if outcome.status == "done":
            return clip(outcome.result["answer"], 4000)
        return f"The helper could not answer ({outcome.status}). Investigate yourself."

    return explore


class Cancelled(Exception):
    pass


def memory_path(ctx: Context, repo_name: str) -> Path:
    return ctx.home / "memory" / f"{repo_name}.md"


def read_memory(ctx: Context, repo_name: str) -> str:
    path = memory_path(ctx, repo_name)
    return path.read_text()[:prompts.MEMORY_CHARS] if path.exists() else ""


def update_memory(ctx: Context, ticket: dict, repo: Repo, work: Path, directory: Path, round_: int, notes: Path,
                  plan: dict | None, summary: str, changed: list[str]) -> None:
    """After an accepted round, fold what was learned into the repository's guide. Never blocks the ticket."""
    current = read_memory(ctx, repo.name)
    task = prompts.memory_task(current, notes.read_text() if notes.exists() else "", plan, summary, changed)
    try:
        agent, run, model = _agent(ctx, ticket, "memory", Workspace(work), (), prompts.SUBMIT_MEMORY, prompts.MEMORY,
                                   directory / "memory", round_, max_steps=3, prompt_chars=len(task) + len(prompts.MEMORY),
                                   thinking_budget=2048)
        outcome = agent.run(task)
        ctx.store.run_end(run, outcome.status, outcome.usage)
    except Exception as exc:  # a failed librarian must not cost an accepted change
        ctx.store.event(ticket["id"], "memory_skipped", "harness", {"error": str(exc)[:300]})
        return
    if outcome.status != "done":
        ctx.store.event(ticket["id"], "memory_skipped", "harness", {"status": outcome.status})
        return
    content = str(outcome.result.get("content", "")).strip()[:prompts.MEMORY_CHARS]
    if content:
        path = memory_path(ctx, repo.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content + "\n")
        temporary.replace(path)
        ctx.store.event(ticket["id"], "memory_updated", f"memory:{model.name}", {"characters": len(content)})


def write_acceptance_tests(ctx: Context, ticket: dict, repo: Repo, work: Path, base: str, sandbox: Sandbox,
                           directory: Path, round_: int, brief: str) -> dict:
    """Tests written from the criteria alone, before any plan or code (Agentless reproduction tests, Otter): they must
    fail now. Anything else the session changed is undone; tests that already pass are dropped."""
    spec = ticket["spec"]
    agent, run, _ = _agent(ctx, ticket, "coding", Workspace(work, sandbox, repo.run_timeout), READ_WRITE, prompts.SUBMIT_TESTS,
                           prompts.TESTWRITER, directory / "acceptance", round_, max_steps=40, nudge_after=15,
                           thinking_budget=4096)
    outcome = agent.run(prompts.testwriter_task(spec, brief, repo.test_command))
    ctx.store.run_end(run, outcome.status, outcome.usage)
    if outcome.status == "cancelled":
        raise Cancelled()
    if outcome.status == "error":
        raise RuntimeError(outcome.error)

    def undo(status: str, path: str) -> None:
        if status == "A":
            (work / path).unlink(missing_ok=True)
        else:
            git(work, "checkout", "-q", base, "--", path)

    rows = gates.changes(work, base)
    for status, path in rows:
        if not repo.is_test(path) or repo.test_config(path) or status == "D":
            undo(status, path)   # the test writer may only add or change tests
    kept, dropped = {}, []
    for status, path in rows:
        if status == "D" or not repo.is_test(path) or repo.test_config(path) or not (work / path).is_file():
            continue
        code, _, _ = sandbox.run(repo.runner(path) or repo.test_command, timeout=repo.run_timeout)
        if code != 0:
            kept[path] = hashlib.sha256((work / path).read_bytes()).hexdigest()
        else:
            undo(status, path)
            dropped.append(path)
    git(work, "reset", "-q")
    record = {"status": outcome.status, "files": kept, "dropped": dropped,
              "tests": (outcome.result or {}).get("tests", []), "untested": (outcome.result or {}).get("untested", "")}
    ctx.store.event(ticket["id"], "acceptance_tests", "harness", {"kept": list(kept), "dropped": dropped})
    return record


def probe_change(ctx: Context, ticket: dict, repo: Repo, work: Path, commit: str, directory: Path, round_: int,
                 brief: str, summary: str, dependencies: list[str] | None, timeout: int) -> list[dict]:
    """After the gates: a tester writes tests from the ticket alone and runs them against the gated result, in a
    scratch checkout. Runs 9-11: changes that passed every gate missed cases the maintainers tested (`async def`,
    `export default`, an extra model call). Only failures the harness reproduces come back, as advice the coder may
    dispute: unlike acceptance tests, a probe cannot lock in a misreading of the ticket."""
    scratch = directory / "probe"
    target = gates.checkout(work, commit, scratch / "work", dependencies or [])
    box = Sandbox(scratch, target, repo.sandbox_env(), trusted=repo.trusted)
    spec = ticket["spec"]
    task = prompts.probe_task(spec, brief, repo.test_command, summary)
    # The coding role: on the review role the prober went to the 9B, whose 16K window cannot hold this work.
    agent, run, _ = _agent(ctx, ticket, "coding", Workspace(target, box, timeout), READ_WRITE, prompts.SUBMIT_PROBES,
                           prompts.PROBE, directory / "probe-agent", round_, max_steps=30, nudge_after=15,
                           prompt_chars=len(task) + len(prompts.PROBE), thinking_budget=CODING_THINKING)
    outcome = agent.run(task)
    ctx.store.run_end(run, outcome.status, outcome.usage)
    if outcome.status == "cancelled":
        raise Cancelled()
    found = []
    for item in (outcome.result or {}).get("probes", []) if outcome.status == "done" else []:
        path = str(item.get("path", ""))
        file = target / path
        if not path or not repo.is_test(path) or not file.is_file() or not file.resolve().is_relative_to(target.resolve()):
            continue
        code, out, _ = box.run(repo.runner(path) or repo.test_command, cwd=target, timeout=timeout)
        if code not in (0, 124):   # the harness reproduces the failure; a claim alone is not a finding
            found.append({**item, "path": path, "source": clip(file.read_text(errors="replace"), 3000),
                          "output": clip(failure_lines(out) or out[-600:], 1500)})
    shutil.rmtree(scratch, ignore_errors=True)
    ctx.store.event(ticket["id"], "probed", "harness", {"round": round_, "status": outcome.status,
                                                         "reported": len((outcome.result or {}).get("probes", [])),
                                                         "reproduced": [f["path"] for f in found]})
    return found


def changed_acceptance(work: Path, acceptance: dict, base: str) -> str:
    """How the coder changed the acceptance tests, for the reviewer; empty when they are untouched."""
    changed = [p for p, digest in acceptance.get("files", {}).items()
               if not (work / p).is_file() or hashlib.sha256((work / p).read_bytes()).hexdigest() != digest]
    return "\n".join(f"{p} was changed or removed by the coder." for p in changed)


def make_plan(ctx: Context, ticket: dict, work: Path, directory: Path, round_: int, brief: str, explorer,
              notes: Path | None = None, acceptance: list[str] = ()) -> tuple[dict, dict]:
    """The sprint contract: a read-only plan, checked mechanically (a test per criterion) and by the reviewer,
    before any edit. Two attempts; after that the coder works from the last plan with the reviewer's concerns."""
    spec = ticket["spec"]
    criteria = set(range(1, len(spec.get("acceptance_criteria") or []) + 1))
    feedback, plan, record = None, None, {"attempts": []}
    for attempt in (1, 2):
        agent, run, planner = _agent(ctx, ticket, "coding", Workspace(work), READ_ONLY, prompts.SUBMIT_PLAN, prompts.PLAN,
                                     directory / f"plan-{attempt}", round_, max_steps=30, explorer=explorer, nudge_after=None,
                                     notes=notes, thinking_budget=6144)
        outcome = agent.run(prompts.plan_task(spec, brief, feedback, acceptance))
        ctx.store.run_end(run, outcome.status, outcome.usage)
        if outcome.status == "cancelled":
            raise Cancelled()
        if outcome.status == "error":
            raise RuntimeError(outcome.error)
        if outcome.status != "done":
            feedback = f"The planning session ended without a plan ({outcome.status}). Be more direct."
            record["attempts"].append({"status": outcome.status})
            continue
        candidate = outcome.result
        uncovered = sorted(criteria - {t.get("criterion") for t in candidate.get("tests", [])})
        if uncovered:
            feedback = f"Criteria {', '.join(map(str, uncovered))} have no test in the plan. Every criterion needs one."
            record["attempts"].append({"status": "uncovered", "criteria": uncovered})
            plan = plan or candidate
            continue
        plan = candidate
        agent, run, reviewer = _agent(ctx, ticket, "review", Workspace(work), READ_ONLY, prompts.SUBMIT_PLAN_REVIEW,
                                      prompts.PLAN_REVIEW, directory / f"plan-review-{attempt}", round_, exclude={planner.name},
                                      max_steps=15)
        verdict = agent.run(prompts.plan_review_task(spec, plan, notes.read_text() if notes and notes.exists() else ""))
        ctx.store.run_end(run, verdict.status, verdict.usage)
        if verdict.status == "cancelled":
            raise Cancelled()
        if verdict.status == "error":
            raise RuntimeError(verdict.error)
        result = verdict.result or {}
        record["attempts"].append({"status": "reviewed", "verdict": result.get("verdict"), "problems": result.get("problems", [])})
        if result.get("verdict") == "approve" or verdict.status != "done":
            record["approved"] = result.get("verdict") == "approve"
            return plan, record
        feedback = "The reviewer found these gaps:\n" + "\n".join(f"- {p}" for p in result.get("problems", []))
    record["approved"] = False
    record["concerns"] = feedback
    return plan or {}, record


# ── intake ──────────────────────────────────────────────────────────────────────────────────────────────

def intake(ctx: Context, ticket: dict) -> dict:
    repo = ctx.repos.get(ticket["repo"])
    directory = ctx.ticket_dir(ticket["id"]) / "intake"
    if directory.exists():
        shutil.rmtree(directory)
    prepare_workspace(Path(repo.path), repo.base, directory / "work")
    agent, run, model = _agent(ctx, ticket, "intake", Workspace(directory / "work"), READ_ONLY, prompts.SUBMIT_TICKET,
                               prompts.INTAKE, directory, 0, max_steps=40)
    outcome = agent.run(prompts.intake_task(ticket["request"], repo.name,
                                            prompts.project_brief(directory / "work", read_memory(ctx, repo.name),
                                                                  ticket["request"])))
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


def accept_round(ctx: Context, ticket: dict, repo: Repo, work: Path, directory: Path, round_: int, root: Path,
                 plan: dict | None, summary: str, report: dict, data: dict, record: dict) -> dict:
    """An accepted round: learn from it, then commit exactly the gated tree and move to ready."""
    spec = ticket["spec"]
    update_memory(ctx, ticket, repo, work, directory, round_, root / "notes.md", plan or None, summary,
                  [path for _, path in report["changed"]])
    # The published commit has exactly the tree the gates tested; a later round stacks on the previous one.
    message = f"{spec['title']}\n\n{neutral(summary)}\n\nTicket: {ticket['id']}"
    head = git(work, "commit-tree", report["tree"], "-p", data.get("head") or data["base"], "-m", message).strip()
    git(work, "reset", "-q", "--soft", head)
    data.pop("fallback", None)
    data.update(head=head, summary=summary, round_dir=str(directory), gates=record["gates"], review=record["review"],
                publish=repo.auto_publish, rounds=data.get("rounds", []) + [record], feedback=None)
    return _move(ctx, ticket, "ready", "ready", {"round": round_, "head": head}, round=round_, data=data, error=None)


def accept_fallback(ctx: Context, ticket: dict, repo: Repo, work: Path, round_: int, root: Path, plan: dict | None,
                    data: dict, failed: dict) -> dict:
    """The round that answered a probe did not pass the gates: accept the earlier gated result instead, and tell the
    person which probes it still fails."""
    fallback = data.pop("fallback")
    git(work, "read-tree", "-u", "--reset", fallback["report"]["tree"])
    git(work, "clean", "-fdq")   # ignored dependency directories stay
    probes = "\n".join(f"- {p['path']} :: {p['test']} ({p['requirement']})" for p in fallback["probes"])
    record = {**fallback["record"], "review": {
        "model": None, "verdict": "not reviewed",
        "summary": ("Quick profile: no model review. The harness gates passed. A tester's tests, written from the ticket, "
                    f"fail on this change:\n{probes}\nThe attempt to address them (round {round_}) did not pass the gates, "
                    f"so this is the round {fallback['round']} result. Please check these cases.")}}
    data["rounds"] = data.get("rounds", []) + [failed]
    ctx.store.event(ticket["id"], "probe_fallback", "harness", {"round": round_, "accepted_round": fallback["round"]})
    return accept_round(ctx, ticket, repo, work, Path(fallback["dir"]), round_, root, plan, fallback["summary"],
                        fallback["report"], data, record)


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
    if "suite_seconds" not in data:
        # Time the base suite once: a hung run cost the coder the full five-minute limit for a ten-second suite.
        code, out, seconds = sandbox.run(repo.test_command, timeout=repo.run_timeout)
        data["suite_seconds"] = round(seconds) if code == 0 else None
        # Run 9: a base that already failed left the coder retrying finish against failures it had not caused.
        data["base_failures"] = (failure_lines(out) or out[-600:]) if code else ""
        ctx.store.merge_data(ticket["id"], suite_seconds=data["suite_seconds"], base_failures=data["base_failures"])
    timeout = run_timeout(repo, data.get("suite_seconds"))

    brief = prompts.project_brief(work, read_memory(ctx, repo.name), prompts.ticket_query(spec))
    explorer = _explorer(ctx, ticket, Workspace(work), directory / "coding", round_)
    if repo.wants_acceptance_tests(spec) and "acceptance" not in data:
        try:
            data["acceptance"] = write_acceptance_tests(ctx, ticket, repo, work, data["base"], sandbox, directory, round_, brief)
        except Cancelled:
            return ctx.store.get(ticket["id"])
        ctx.store.merge_data(ticket["id"], acceptance=data["acceptance"])
    acceptance = data.get("acceptance") or {}
    plan = data.get("plan")
    if repo.wants_plan(spec) and not plan:
        try:
            plan, plan_record = make_plan(ctx, ticket, work, directory, round_, brief, explorer, notes=root / "notes.md",
                                          acceptance=list(acceptance.get("files", {})))
        except Cancelled:
            return ctx.store.get(ticket["id"])
        data.update(plan=plan, plan_record=plan_record)
        ctx.store.merge_data(ticket["id"], plan=plan, plan_record=plan_record)
        ctx.store.event(ticket["id"], "planned", "harness", {"approved": plan_record.get("approved"),
                                                             "attempts": len(plan_record["attempts"])})
        if plan_record.get("concerns"):
            data["feedback"] = ((data.get("feedback") or "") + "\n\nThe plan reviewer's remaining concerns (address them):\n"
                                + plan_record["concerns"]).strip()
    agent, run, coder = _agent(ctx, ticket, "coding", Workspace(work, sandbox, timeout), READ_WRITE, prompts.FINISH,
                               prompts.CODING, directory / "coding", round_, max_steps=repo.coding_steps_for(spec), nudge_after=15,
                               notes=root / "notes.md", explorer=explorer, evidence=True, verify=repo.test_command,
                               # Run 7: 75% of the coder's output was reasoning, most of it in a few steps that ran to
                               # the 4,096 cap; Aider found Qwen3 edits as well or better without thinking.
                               thinking_budget=repo.coding_thinking)
    outcome = agent.run(prompts.coding_task(spec, {"test_command": repo.test_command, "build_command": repo.build_command,
                                                   "base_failures": data.get("base_failures", "")},
                                            data.get("feedback"), brief, plan or None, list(acceptance.get("files", {}))))
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
    ctx.store.event(ticket["id"], "gates", "harness", {"round": round_, "passed": report["passed"], "checks": record["gates"],
                                                        "failures": {c["name"]: failure_lines(c.get("detail") or "") or (c.get("detail") or "")[-600:]
                                                                     for c in report["checks"] if not c["ok"]}})
    summary = (outcome.result or {}).get("summary") or f"(the coding agent stopped without a summary: {outcome.status})"

    if report["passed"] and not repo.wants_review(spec):
        # Quick profile: the gates are the machine check; the person reviews intent in the PR.
        found = []
        if repo.wants_probe(spec) and not data.get("probed") and round_ < ticket["max_rounds"]:
            data["probed"] = True   # once per ticket: the probe is advice, not a loop
            ctx.store.merge_data(ticket["id"], probed=True)
            try:
                found = probe_change(ctx, ticket, repo, work, report["commit"], directory, round_, brief, summary,
                                     data.get("dependencies"), timeout)
            except Cancelled:
                return ctx.store.get(ticket["id"])
            record["probe"] = {"reproduced": [f["path"] for f in found]}
        if not found:
            record["review"] = {"model": None, "verdict": "not reviewed",
                                "summary": "Quick profile: no model review. The harness gates passed; please review the change."}
            return accept_round(ctx, ticket, repo, work, directory, round_, root, plan, summary, report, data, record)
        feedback = prompts.probe_feedback(found)
        # Run 12: the round after a probe broke the gates and the ticket failed, losing a result that had passed
        # them. The probe may only improve a result: keep this one to fall back on.
        data["fallback"] = {"round": round_, "dir": str(directory), "summary": summary, "record": record,
                            "report": {"tree": report["tree"], "changed": report["changed"]},
                            "probes": [{"path": f["path"], "test": f.get("test", ""), "requirement": f.get("requirement", "")}
                                       for f in found]}
    elif report["passed"]:
        ctx.renew()
        reviewer_ws = Workspace(work)
        diff_text = gates.diff(work, data["base"], report["commit"])
        # Eval: a plan reviewer predicted the bug that later failed the hidden tests; its concern reached the coder but
        # not the final reviewer, who approved. Unresolved plan concerns are now the final review's checklist.
        concerns = (data.get("plan_record") or {}).get("concerns")
        edited = changed_acceptance(work, acceptance, data["base"])
        if edited:
            # Editing an acceptance test is allowed (it may be wrong), but never silently: the reviewer judges it.
            concerns = ((concerns or "") + "\n- The coder changed acceptance tests written from the criteria before coding. "
                        "A change that weakens what they check is a blocking finding:\n" + edited).strip()
        review_text = prompts.review_task(spec, clip(diff_text, DIFF_CHARS), gates.summary(report), summary, brief,
                                          plan or None, concerns)
        # Same-model reviewers share the coder's blind spots: prefer another model when one fits the review.
        agent, run, reviewer = _agent(ctx, ticket, "review", reviewer_ws, READ_ONLY, prompts.SUBMIT_REVIEW, prompts.REVIEW,
                                      directory / "review", round_, exclude={coder.name},
                                      prompt_chars=len(review_text) + len(prompts.REVIEW), max_steps=30)
        review = agent.run(review_text)
        ctx.store.run_end(run, review.status, review.usage)
        if review.status in ("out_of_steps", "stalled", "looping"):
            # A review that never reached a verdict says nothing about the change: try once more, fresh.
            agent, run, reviewer = _agent(ctx, ticket, "review", reviewer_ws, READ_ONLY, prompts.SUBMIT_REVIEW, prompts.REVIEW,
                                          directory / "review-2", round_, exclude={coder.name},
                                          prompt_chars=len(review_text) + len(prompts.REVIEW), max_steps=30)
            review = agent.run(review_text + "\n\nBe efficient: read only what you need to judge the criteria, then submit.")
            ctx.store.run_end(run, review.status, review.usage)
        if review.status == "cancelled":
            return ctx.store.get(ticket["id"])
        if review.status == "error":
            raise RuntimeError(review.error)
        verdict = (review.result or {}).get("verdict") if review.status == "done" else None
        if verdict == "changes" and not (review.result or {}).get("findings"):
            verdict = "approve"  # only blocking findings send a change back; notes alone reach the person in the PR
        record["review"] = {"model": reviewer.name, "status": review.status, **(review.result or {})}
        if verdict == "approve":
            return accept_round(ctx, ticket, repo, work, directory, round_, root, plan, summary, report, data, record)
        feedback = "The reviewer requested changes:\n" + "\n".join(
            f"- {f.get('file')}: {f.get('problem')} Fix: {f.get('fix')}" for f in (review.result or {}).get("findings", [])) \
            if verdict == "changes" else f"The review did not complete ({review.status}); make sure the change is clear and complete."
    else:
        if data.get("fallback"):
            return accept_fallback(ctx, ticket, repo, work, round_, root, plan, data, record)
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
    lines += ["", "## Review", (f"Model review ({review.get('model', 'unknown')}): {review.get('verdict', 'none')}."
                                if review.get("model") else "No model review (quick profile)."),
              neutral(str(review.get("summary", "")).strip())]
    if review.get("notes"):
        lines += ["", "Reviewer notes (not blocking):"] + [f"- {neutral(str(n))}" for n in review["notes"]]
    lines += ["",
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
