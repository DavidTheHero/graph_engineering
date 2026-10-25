"""graph-engineer: register repositories, file tickets, act on them, run workers, serve the control centre, run evals."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from . import workflow
from .llm import Registry, probe
from .repos import Repo, Repos, detect
from .store import Store

MODELS_EXAMPLE = {
    "models": {
        "qwen27b": {"endpoint": "http://192.168.0.60:18020/v1", "model_id": "qwen3.8-27b", "context": 150000, "max_output": 12288},
        "qwen9b": {"endpoint": "http://192.168.0.62:18035/v1", "model_id": "docker.io/local/qwen35-9b:16k", "context": 16384,
                   "max_output": 4096, "think": False}},
    "roles": {"intake": ["qwen27b"], "coding": ["qwen27b"], "review": ["qwen27b"], "explore": ["qwen27b", "qwen9b"],
              "memory": ["qwen27b"]},  # the 9B wrote the ticket's solution into the guide despite instructions
}


def home_dir(value: str | None) -> Path:
    return Path(value or os.environ.get("GE_HOME") or Path.home() / ".graph_engineering").expanduser().resolve()


def actor() -> str:
    return "person:" + (os.environ.get("USER") or "local")


def show(value) -> None:
    print(json.dumps(value, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graph-engineer", description=__doc__)
    parser.add_argument("--home", help="state directory (default $GE_HOME or ~/.graph_engineering)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the state directory and an example models.json")
    sub.add_parser("models", help="check each configured model endpoint")

    repo = sub.add_parser("repo", help="register or show repositories").add_subparsers(dest="action", required=True)
    add = repo.add_parser("add")
    add.add_argument("name")
    add.add_argument("path")
    add.add_argument("--slug", help="owner/name on GitHub (detected from origin)")
    add.add_argument("--local", action="store_true", help="never publish to GitHub")
    add.add_argument("--base")
    add.add_argument("--test", dest="test_command")
    add.add_argument("--build", dest="build_command")
    add.add_argument("--auto-accept", action="store_true")
    add.add_argument("--set", action="append", default=[], metavar="KEY=JSON", help="any other field, e.g. max_rounds=4")
    repo.add_parser("list")
    repo.add_parser("show").add_argument("name")

    ticket = sub.add_parser("ticket", help="file and act on tickets").add_subparsers(dest="action", required=True)
    new = ticket.add_parser("new")
    new.add_argument("repo")
    new.add_argument("request", nargs="?", help="what you want, in your own words")
    new.add_argument("--spec", help="a JSON file with title, kind, problem, acceptance_criteria: skips intake")
    ticket.add_parser("list").add_argument("--state")
    ticket.add_parser("show").add_argument("id")
    accept = ticket.add_parser("accept")
    accept.add_argument("id")
    accept.add_argument("--allow-test-config", action="store_true", help="let this ticket change test configuration")
    for name in ("approve-publish", "retry"):
        ticket.add_parser(name).add_argument("id")
    for name in ("reject", "answer", "request-changes", "cancel"):
        p = ticket.add_parser(name)
        p.add_argument("id")
        p.add_argument("text", nargs="?", default="")
    merge = ticket.add_parser("approve-merge", help="approve merging the PR's current head commit")
    merge.add_argument("id")
    merge.add_argument("--head", help="the commit you reviewed (default: the PR head, shown for confirmation)")

    serve = sub.add_parser("serve", help="run workers and the control centre")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--workers", type=int, default=1)
    work = sub.add_parser("work", help="run workers without the control centre")
    work.add_argument("--workers", type=int, default=1)
    evals = sub.add_parser("eval", help="run eval tasks through the development round")
    evals.add_argument("tasks", nargs="+")
    evals.add_argument("--repeat", type=int, default=1)
    evals.add_argument("--out", help="results directory (default <home>/evals/<timestamp>)")
    evals.add_argument("--profile", choices=["quick", "thorough"], help="override every repository's profile")
    evals.add_argument("--probe", action=argparse.BooleanOptionalAction, default=None,
                       help="override every repository's probe setting (quick profile)")
    evals.add_argument("--coding-thinking", type=int, help="the coder's reasoning tokens per turn; 0 turns thinking off; negative thinks only when stuck (the default, -2048)")
    mine = sub.add_parser("eval-mine", help="turn past commits with tests into validated eval tasks")
    mine.add_argument("repo")
    mine.add_argument("--ref", help="branch to mine (default: the repository's base)")
    mine.add_argument("--limit", type=int, default=10)
    mine.add_argument("--out", default="evals/tasks/mined")
    mine.add_argument("--no-validate", action="store_true", help="skip running the reference solution")
    sub.add_parser("mcp", help="serve MCP over stdio (needs the mcp extra)")

    args = parser.parse_args(argv)
    home = home_dir(args.home)
    home.mkdir(parents=True, exist_ok=True)
    models_path = home / "models.json"
    if args.command == "init":
        if not models_path.exists():
            models_path.write_text(json.dumps(MODELS_EXAMPLE, indent=2) + "\n")
        print(f"state directory: {home}\nedit {models_path}, then: graph-engineer repo add NAME PATH")
        return 0
    store, repos = Store(home / "ge.db"), Repos(home)
    registry = lambda: Registry.load(models_path)

    if args.command == "models":
        reg = registry()
        for name, model in reg.models.items():
            print(name, model.endpoint, model.model_id, probe(model))
        show(reg.roles)
        return 0

    if args.command == "repo":
        if args.action == "add":
            found = detect(Path(args.path))
            fields = {k: v for k, v in found.items() if v not in (None, "", [])}
            fields.update({k: getattr(args, k) for k in ("slug", "base", "test_command", "build_command") if getattr(args, k)})
            if args.local:
                fields["slug"] = None
            if args.auto_accept:
                fields["auto_accept"] = True
            for item in args.set:
                key, _, value = item.partition("=")
                fields[key] = json.loads(value)
            show(repos.save(Repo(name=args.name, path=str(Path(args.path).resolve()), **fields)).public())
        elif args.action == "list":
            for r in repos.all():
                print(f"{r.name:24} {r.slug or '(local)':36} {r.path}")
        else:
            show(repos.get(args.name).public())
        return 0

    if args.command == "ticket":
        return ticket_command(args, store, repos)

    if args.command in ("serve", "work"):
        from .scheduler import Scheduler
        ctx = workflow.Context(home, store, repos, registry())
        scheduler = Scheduler(ctx, workers=args.workers)
        scheduler.start()
        try:
            if args.command == "serve":
                from .web import serve
                serve(ctx, scheduler, port=args.port)
            else:
                while True:
                    time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            scheduler.shutdown()
        return 0

    if args.command == "eval":
        from .evaluation import run
        out = Path(args.out) if args.out else home / "evals" / time.strftime("%Y%m%d-%H%M%S")
        summary = run([Path(t) for t in args.tasks], repos, registry(), out, args.repeat, profile=args.profile,
                      probe=args.probe, coding_thinking=args.coding_thinking)
        show(summary)
        print(f"results: {out}")
        return 0 if summary["pass_rate"] == 1 else 1

    if args.command == "eval-mine":
        from .mining import mine
        repo = repos.get(args.repo)
        written = mine(repo, args.ref or repo.base, Path(args.out), args.limit, check=not args.no_validate)
        print(f"{len(written)} task(s) written to {args.out}")
        return 0

    if args.command == "mcp":
        from .mcp_server import serve_stdio
        serve_stdio(store, repos)
        return 0
    return 2


def ticket_command(args, store: Store, repos: Repos) -> int:
    who = actor()
    if args.action == "new":
        repo = repos.get(args.repo)
        if args.spec:
            spec = json.loads(Path(args.spec).read_text())
            ticket = store.create(repo.name, args.request or spec["title"], who, repo.max_rounds, spec=spec, state="proposed")
        elif args.request:
            ticket = store.create(repo.name, args.request, who, repo.max_rounds)
        else:
            print("give a request or --spec", file=sys.stderr)
            return 2
        print(ticket["id"], ticket["state"])
        return 0
    if args.action == "list":
        for t in store.list(args.state):
            print(f"{t['id']}  {t['state']:15} {'working' if t['working'] else '       '}  {t['repo']:20} "
                  f"{(t['spec'].get('title') or t['request'])[:70]}")
        return 0
    if args.action == "show":
        ticket = store.get(args.id)
        show({**ticket, "events": [e for e in store.events(args.id) if e["kind"] != "agent"][-30:]})
        return 0
    if args.action == "approve-merge":
        ticket = store.get(args.id)
        head = args.head or (ticket["data"].get("pr") or {}).get("head")
        if not args.head:
            pr = ticket["data"].get("pr") or {}
            answer = input(f"Merge {pr.get('url') or pr.get('branch')} at {head}? [y/N] ")
            if answer.strip().lower() != "y":
                return 1
        show(workflow.approve_merge(store, args.id, who, head)["state"])
        return 0
    action = {"accept": lambda: workflow.accept(store, repos, args.id, who,
                                                {"allow_test_config": True} if args.allow_test_config else None),
              "approve-publish": lambda: workflow.approve_publish(store, args.id, who),
              "retry": lambda: workflow.retry(store, args.id, who),
              "reject": lambda: workflow.reject(store, args.id, who, args.text),
              "answer": lambda: workflow.answer(store, args.id, who, args.text),
              "request-changes": lambda: workflow.request_changes(store, args.id, who, args.text),
              "cancel": lambda: workflow.cancel(store, args.id, who, args.text)}[args.action]
    print(action()["state"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
