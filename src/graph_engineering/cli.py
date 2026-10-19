"""Reusable command line for bounded, durable graph execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

from .evaluation import evaluate_reports, seed_repositories
from .models import detect_opencode, probe_endpoints
from .runtime import BusyError, Store, redact


DEFAULT_HOME = Path(__file__).resolve().parents[2] / ".runtime"


def _workflow(home: Path):
    from .workflow import Workflow
    return Workflow(home)


def _initialise(*args, **kwargs):
    from .workflow import initialise
    return initialise(*args, **kwargs)


def _contracts():
    from .workflow import graph_contracts
    return graph_contracts()


def _argv(value: str) -> list[str]:
    try:
        result = json.loads(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("test command must be JSON argv, e.g. [\"python\",\"-m\",\"unittest\"]") from exc
    if not isinstance(result, list) or not result or any(not isinstance(item, str) or not item for item in result):
        raise argparse.ArgumentTypeError("test command must be a nonempty JSON list of nonempty strings")
    return result


def _poll(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("poll must be a number of seconds") from exc
    if not 0 < result <= 30:
        raise argparse.ArgumentTypeError("poll must be greater than zero and at most 30 seconds")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="graph-engineering", description=__doc__)
    result.add_argument("--home", type=Path, default=DEFAULT_HOME, help="controller state/artifact directory")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="inspect a repository and create isolated controller configuration")
    init.add_argument("repo", type=Path)
    init.add_argument("--backend", choices=("local", "github"), default="local")
    init.add_argument("--slug", help="explicit GitHub owner/repository")
    init.add_argument("--trust-local-code", action="store_true", help="permit this repository's code to run locally")
    init.add_argument("--auto-merge", action="store_true", help="explicitly enable trusted low-risk automatic merge policy")
    init.add_argument("--base", help="base branch override")
    init.add_argument("--routing", choices=("baseline", "multimodel"), default="baseline", help="A-only measured baseline or two-model roles")
    init.add_argument("--test-command", type=_argv, help="explicit JSON argv; never shell text")
    for name in ("scan", "execute", "enqueue"):
        command = commands.add_parser(name, help="create a run and " + ("collect validated evidence" if name == "scan" else "execute its graph"))
        command.add_argument("--config", type=Path, required=True)
        if name == "execute":
            command.add_argument("--until", help="stop at the requested graph node boundary")
    resume = commands.add_parser("resume", help="resume a durable run, explicitly clearing a stop request")
    resume.add_argument("run")
    resume.add_argument("--until")
    status = commands.add_parser("status", help="inspect runs and optionally persisted transition events")
    status.add_argument("run", nargs="?")
    status.add_argument("--events", action="store_true")
    stop = commands.add_parser("stop", help="persist a cancellation request checked by the controller")
    stop.add_argument("run")
    approval = commands.add_parser("approve", help="record human approval for exactly the independently tested head")
    approval.add_argument("run")
    approval.add_argument("--head", required=True)
    approval.add_argument("--actor", required=True)
    commands.add_parser("doctor", help="observe OpenCode version and local model endpoint health")
    commands.add_parser("graph", help="export inspectable node contracts and transitions")
    seed = commands.add_parser("seed", help="create fresh Python and JavaScript real Git defect fixtures")
    seed.add_argument("--output", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="aggregate verified report measurements and an optional baseline")
    evaluate.add_argument("--reports", type=Path, required=True)
    evaluate.add_argument("--baseline", type=Path)
    evaluate.add_argument("--output", type=Path)
    serve = commands.add_parser("serve", help="run a restartable foreground controller; external service manager optional")
    serve.add_argument("--poll", type=_poll, default=5)
    serve.add_argument("--once", action="store_true", help="process one sweep and return")
    ui = commands.add_parser("ui", help="open a local, read-only workflow dashboard")
    ui.add_argument("--port", type=int, default=8765, help="loopback HTTP port (default: 8765)")
    dispatch = commands.add_parser('dispatch', help='run the durable priority-aware ticket scheduler')
    dispatch.add_argument('--workers',type=int,default=2)
    dispatch.add_argument('--once',action='store_true')
    worker = commands.add_parser('worker-step',help=argparse.SUPPRESS)
    worker.add_argument('ticket'); worker.add_argument('owner')
    centre = commands.add_parser('centre',help='start the operator control centre and scheduler')
    centre.add_argument('--port',type=int,default=8765)
    centre.add_argument('--workers',type=int,default=2)
    centre.add_argument('--attach-controller',action='store_true',help='serve the UI against an existing dispatch service without launching another')
    return result


def _emit(value, *, compact: bool = False, error: bool = False) -> None:
    print(json.dumps(redact(value), indent=None if compact else 2, ensure_ascii=False, default=str),
          file=sys.stderr if error else sys.stdout, flush=True)


def _summary(run: dict) -> dict:
    result = {key: run.get(key) for key in ("id", "node", "status", "state")}
    if run.get("cancel"):
        result["cancellation_requested"] = True
    return result


def _run_exit(run: dict) -> int:
    return 2 if run.get("status") in ("blocked", "failed", "escalated") else 0


def _reports(path: Path) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, dict) and "reports" in value:
        value = value["reports"]
    if not isinstance(value, list):
        raise ValueError("evaluation input must be a JSON list of measured reports")
    return value


def _serve(workflow, *, poll: float, once: bool) -> int:
    while True:
        for run in workflow.store.list_runs():
            eligible = run["status"] in ("ready", "running")
            if run["status"] == "waiting_approval":
                head = run["state"].get("head")
                eligible = bool(head) and workflow.store.approved_head(run["id"]) == head
            if not eligible:
                continue
            try:
                _emit(_summary(workflow.execute(run["id"])), compact=True)
            except BusyError as exc:
                _emit({"run": run["id"], "status": "busy", "error": str(exc)}, compact=True, error=True)
        if once:
            return 0
        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.home = args.home.resolve()
    store = None
    try:
        if args.command == "init":
            config = _initialise(args.repo.resolve(), args.home, backend=args.backend,
                trusted_local=args.trust_local_code, slug=args.slug, test_command=args.test_command,
                auto_merge=args.auto_merge, base=args.base, routing=args.routing)
            _emit({"config": str(config)})
        elif args.command in ("scan", "execute", "enqueue", "resume", "serve"):
            workflow = _workflow(args.home)
            store = workflow.store
            if args.command == "serve":
                return _serve(workflow, poll=args.poll, once=args.once)
            if args.command == "resume":
                store.get(args.run)
                store.clear_stop(args.run)
                run_id, until = args.run, args.until
            else:
                run_id = workflow.new_run(args.config.resolve())["id"]
                if args.command == "enqueue":
                    from .tickets import Tickets
                    tickets = Tickets(args.home)
                    try: tickets.admit(run_id)
                    finally: tickets.store.close()
                    _emit(_summary(store.get(run_id)))
                    return 0
                until = "task_planning" if args.command == "scan" else args.until
            run = workflow.execute(run_id, until=until)
            _emit(_summary(run))
            return _run_exit(run)
        elif args.command in ("status", "stop", "approve"):
            store = Store(args.home)
            if args.command == "status":
                if args.run:
                    run = store.get(args.run)
                    _emit({"run": _summary(run), "events": store.events(args.run)} if args.events else _summary(run))
                else:
                    _emit([{"run": _summary(run), "events": store.events(run["id"])} if args.events else _summary(run)
                           for run in store.list_runs()])
            elif args.command == "stop":
                store.get(args.run)
                store.stop(args.run)
                _emit(_summary(store.get(args.run)))
            else:
                if not re.fullmatch(r"[a-fA-F0-9]{40,64}", args.head) or not args.actor.strip():
                    raise ValueError("approval needs a complete commit SHA and nonempty human actor")
                store.approve(args.run, args.head, args.actor.strip())
                _emit({"run": args.run, "approved_head": args.head, "actor": args.actor.strip()})
        elif args.command == "doctor":
            try:
                executable, version = detect_opencode()
                opencode = {"available": True, "executable": executable, "version": version}
            except Exception as exc:
                opencode = {"available": False, "error": str(exc)}
            report = {"opencode": opencode, **probe_endpoints(args.home / "health")}
            _emit(report)
            if not opencode["available"] or any(not model.get("available") for model in report["models"].values()):
                return 2
        elif args.command == "graph":
            _emit(_contracts())
        elif args.command == "ui":
            from .dashboard import serve_dashboard
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            serve_dashboard(args.home, args.port)
        elif args.command == 'dispatch':
            from .scheduler import serve_scheduler
            serve_scheduler(args.home,args.workers,args.once)
        elif args.command == 'worker-step':
            from .scheduler import execute_assignment
            return execute_assignment(args.home,args.ticket,args.owner)
        elif args.command == 'centre':
            from .dashboard import serve_centre
            if not 1 <= args.port <= 65535 or not 1 <= args.workers <= 8:
                raise ValueError('Invalid port or worker concurrency')
            serve_centre(args.home,args.port,args.workers,attach_controller=args.attach_controller)
        elif args.command == "seed":
            repositories = seed_repositories(args.output)
            _emit({"repositories": [str(repo) for repo in repositories],
                   "evidence": str(args.output.resolve() / "seed-manifest.json")})
        elif args.command == "evaluate":
            result = evaluate_reports(_reports(args.reports), _reports(args.baseline) if args.baseline else None)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            _emit(result)
        return 0
    except KeyboardInterrupt:
        _emit({"status": "controller_interrupted", "message": "Durable state retained; use resume or serve."}, error=True)
        return 130
    except Exception as exc:
        _emit({"error": type(exc).__name__ + ": " + str(exc)}, error=True)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
