"""Reusable command line for bounded, durable graph execution."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
import re
import sys
import time

from .evaluation import evaluate_reports, seed_repositories
from .models import detect_opencode, probe_endpoints
from .runtime import BusyError, Store, redact


# GRAPH_ENGINEERING_HOME lets an installed (non-editable) package keep state outside site-packages.
DEFAULT_HOME = Path(os.environ.get("GRAPH_ENGINEERING_HOME") or Path(__file__).resolve().parents[2] / ".runtime")


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
    result = argparse.ArgumentParser(prog="graph-engineer", description=__doc__)
    result.add_argument("--home", type=Path, default=DEFAULT_HOME, help="controller state/artifact directory")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="inspect a repository and create isolated controller configuration")
    init.add_argument("repo", type=Path)
    init.add_argument("--backend", choices=("local", "github"), default="local")
    init.add_argument("--slug", help="explicit GitHub owner/repository")
    init.add_argument("--trust-local-code", action="store_true", help="permit this repository's code to run locally")
    init.add_argument("--auto-merge", action="store_true", help="explicitly enable trusted low-risk automatic merge policy")
    init.add_argument("--base", help="base branch override")
    init.add_argument("--routing", choices=("baseline", "multimodel", "assigned"), default=None,
                      help="large-tier only, role tiers, or the applied measured assignment (default: assigned if applied, else baseline)")
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
    ui = commands.add_parser("ui", help="open the local dashboard (read-only unless --control)")
    ui.add_argument("--port", type=int, default=8765, help="loopback HTTP port (default: 8765)")
    ui.add_argument("--control", action="store_true", help="serve the operator control centre and start the scheduler")
    ui.add_argument("--workers", type=int, default=2, help="scheduler concurrency with --control")
    repo = commands.add_parser("repo", help="register and inspect repositories (metadata access only)")
    repo_commands = repo.add_subparsers(dest="repo_command", required=True)
    repo_add = repo_commands.add_parser("add", help="register a local Git root; grants no execution or publication")
    repo_add.add_argument("path", type=Path)
    repo_commands.add_parser("list", help="list registered repositories")
    repo_inspect = repo_commands.add_parser("inspect", help="detected ecosystems and commands; executes nothing")
    repo_inspect.add_argument("repository_id")
    ticket = commands.add_parser("ticket", help="create natural-language tickets")
    ticket_commands = ticket.add_subparsers(dest="ticket_command", required=True)
    ticket_create = ticket_commands.add_parser("create", help="persist a plain-language problem and queue investigation")
    ticket_create.add_argument("repository_id")
    ticket_create.add_argument("description")
    ticket_create.add_argument("--priority", choices=("P0", "P1", "P2", "P3"))
    ticket_create.add_argument("--expected", default="")
    ticket_create.add_argument("--logs", default="")
    start = commands.add_parser("run", help="queue a workflow for a registered repository (processed by dispatch/ui --control)")
    start.add_argument("repository_id")
    start.add_argument("--mode", choices=("read_only", "assisted"), default="read_only")
    start.add_argument("--inspect-with-model", action="store_true", help="read-only model inspection of source")
    start.add_argument("--trust-execution", action="store_true", help="explicitly permit this repository's code to run (assisted)")
    models = commands.add_parser("models", help="list the model registry and verify advertised identifiers")
    model_commands = models.add_subparsers(dest="models_command")
    assign = model_commands.add_parser("assign", help="probe a set of models and assign them to graph roles")
    assign.add_argument("--endpoint", action="append", default=[], help="OpenAI-compatible base URL (repeatable); "
                        "default: the endpoints in the current registry")
    assign.add_argument("--model", action="append", default=[], metavar="URL=MODEL_ID",
                        help="assess only these model ids on an endpoint (repeatable); default: every advertised model")
    assign.add_argument("--trials", type=int, default=3, help="probe calls per test (1-10)")
    assign.add_argument("--apply", action="store_true", help="write models.json/routing.json and update registered repositories")
    assign.add_argument("--plan", type=Path, help="apply a previously saved plan instead of probing again")
    assign.add_argument("--allow-partial", action="store_true", help="apply although some models were unreachable")
    assign.add_argument("--allow-uncovered", action="store_true", help="apply although some roles have no eligible model")
    mcp = commands.add_parser("mcp", help="Model Context Protocol server")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser("serve", help="serve MCP over stdio (default) or loopback HTTP")
    mcp_serve.add_argument("--profile", choices=("read", "operate", "admin"), default="read")
    mcp_serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    mcp_serve.add_argument("--port", type=int, default=8787)
    mcp_serve.add_argument("--client", default="mcp", help="client label recorded in the audit trail")
    mcp_config = mcp_commands.add_parser("config", help="print a client configuration snippet")
    mcp_config.add_argument("--client", choices=("codex", "opencode", "json"), default="json")
    mcp_config.add_argument("--profile", choices=("read", "operate", "admin"), default="read")
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
            eligible = run["status"] in ("ready", "running") and run["state"].get("retry_not_before", 0) <= time.time()
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


def _version(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else None


def _doctor(home: Path) -> dict:
    """Validate the environment. Required: git, OpenCode 2.0.32 and at least one model with a verified id."""
    from .models import discover_models, load_models
    checks: dict = {"python": sys.version.split()[0], "home": str(home)}
    checks["git"] = _version(["git", "--version"])
    try:
        executable, version = detect_opencode()
        checks["opencode"] = {"available": True, "executable": executable, "version": version}
    except Exception as exc:
        checks["opencode"] = {"available": False, "error": str(exc)}
    checks["gh"] = {"version": _version(["gh", "--version"]),
                    "authenticated": shutil.which("gh") is not None and subprocess.run(
                        ["gh", "auth", "status"], capture_output=True, timeout=15).returncode == 0}
    checks["node"] = _version(["node", "--version"])
    try:
        models = load_models(home)
        checks["models"] = discover_models(models, timeout=6)
        probe_endpoints(home / "health", models)
    except Exception as exc:
        checks["models"] = {"error": str(exc)}
    try:
        import anyio
        from mcp import Client
        from .mcp_server import build_server

        async def handshake():
            async with Client(build_server(home, "read", "doctor")) as client:
                return client.protocol_version, len((await client.list_tools()).tools)
        protocol, count = anyio.run(handshake)
        checks["mcp"] = {"available": True, "protocol_version": protocol, "read_tools": count}
    except ImportError as exc:
        checks["mcp"] = {"available": False, "error": str(exc)}
    except Exception as exc:
        checks["mcp"] = {"available": False, "error": type(exc).__name__ + ": " + str(exc)}
    verified = [k for k, m in checks["models"].items() if isinstance(m, dict) and m.get("identifier_verified")] \
        if isinstance(checks["models"], dict) and "error" not in checks["models"] else []
    checks["ready"] = bool(checks["git"] and checks["opencode"]["available"] and verified)
    checks["actions"] = [message for failed, message in [
        (not checks["git"], "Install Git."),
        (not checks["opencode"]["available"], "Install OpenCode 2.0.32 (the provider configuration is verified for it)."),
        (not verified, "No configured model id is advertised by its server; run `graph-engineer models` and fix models.json."),
        (not checks["mcp"]["available"], "Optional: pip install 'graph-engineering[mcp]' to enable the MCP server."),
        (not checks["gh"]["authenticated"], "Optional: `gh auth login` for live GitHub publication."),
    ] if failed]
    return checks


def _platform_command(args) -> int:
    from .tickets import Tickets
    from . import control_repositories as repositories
    if args.command == "mcp":
        return _mcp(args)
    if args.command == "models" and args.models_command == "assign":
        return _assign_models(args)
    if args.command == "models":
        from .models import discover_models, load_models
        _emit(discover_models(load_models(args.home)))
        return 0
    tickets = Tickets(args.home)
    try:
        if args.command == "repo" and args.repo_command == "add":
            registered = repositories.register(tickets, str(args.path.resolve()))
            _emit(registered | {"next": "Metadata access only. Use `run` or the control centre to start work; "
                                        "code execution needs explicit trust."})
        elif args.command == "repo" and args.repo_command == "list":
            _emit(repositories.repositories(tickets))
        elif args.command == "repo":
            _emit(repositories.inspect(tickets, args.repository_id))
        elif args.command == "ticket":
            from .report_store import create
            _emit(create(tickets, {"repository_id": args.repository_id, "description": args.description,
                                   "priority": args.priority, "expected": args.expected, "logs": args.logs},
                         "cli-" + uuid.uuid4().hex))
        else:
            data = {"mode": args.mode, "inspect_with_model": args.inspect_with_model,
                    "trust_execution": args.trust_execution, "scan_only": args.mode == "assisted"}
            _emit(repositories.start(tickets, args.repository_id, data, "cli-" + uuid.uuid4().hex)
                  | {"next": "Queued. Process with `graph-engineer dispatch` or `graph-engineer ui --control`."})
    finally:
        tickets.store.close()
    return 0


def _assign_models(args) -> int:
    from . import model_assignment as assignment
    from .models import load_models
    if args.plan:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    else:
        if not 1 <= args.trials <= 10:
            raise ValueError("--trials must be 1-10")
        endpoints = args.endpoint or sorted({m["endpoint"].rstrip("/") for m in load_models(args.home).values()})
        select: dict[str, list[str]] = {}
        for item in args.model:
            url, sep, model_id = item.rpartition("=")
            if not sep or not url or not model_id:
                raise ValueError("--model must be URL=MODEL_ID")
            select.setdefault(url.rstrip("/"), []).append(model_id)
            if url.rstrip("/") not in [e.rstrip("/") for e in endpoints]:
                endpoints.append(url)
        plan = assignment.assess(endpoints, home=args.home, select=select, trials=args.trials)
        args.home.mkdir(parents=True, exist_ok=True)
        (args.home / "model-assignment.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    summary = {"complete": plan["complete"], "uncovered_roles": plan["uncovered_roles"], "role_routes": plan["role_routes"],
               "models": {k: {"model_id": p.get("model_id"), "reachable": p["reachable"], "tier": p.get("tier"),
                              "context": p.get("context"), "parameters_b": p.get("parameters_b"),
                              "roles": plan["roles_by_model"].get(k, []), "assessed": p["reachable"] and p.get("assessed", True),
                              **({"rates": {m: round(v, 2) if isinstance(v, float) else v
                                            for m, v in assignment.rates(p).items()}} if p["reachable"] and p.get("assessed", True)
                                 else {"error": p.get("error") or p.get("skipped") or (p.get("infrastructure_errors") or [None])[0]})}
                          for k, p in plan["models"].items()},
               "plan": str(args.plan or args.home / "model-assignment.json")}
    if args.apply:
        summary["applied"] = assignment.apply_plan(plan, args.home, allow_partial=args.allow_partial,
                                                   allow_uncovered=args.allow_uncovered)
    else:
        summary["next"] = "Dry run. Review the plan, then rerun with --apply (or --plan <file> --apply)."
    _emit(summary)
    return 0 if plan["complete"] and not plan["uncovered_roles"] else 2


def _mcp(args) -> int:
    if args.mcp_command == "config":
        executable = shutil.which("graph-engineer") or sys.argv[0]
        argv = [str(Path(executable).resolve()), "--home", str(args.home), "mcp", "serve", "--profile", args.profile]
        if args.client == "codex":
            print("[mcp_servers.graph-engineering]\n" f"command = {json.dumps(argv[0])}\n"
                  f"args = {json.dumps(argv[1:] + ['--client', 'codex'])}\nstartup_timeout_sec = 20\ntool_timeout_sec = 120")
        elif args.client == "opencode":
            _emit({"$schema": "https://opencode.ai/config.json", "mcp": {"graph-engineering": {
                "type": "local", "command": argv + ["--client", "opencode"], "enabled": True, "timeout": 20000}}})
        else:
            _emit({"mcpServers": {"graph-engineering": {"command": argv[0], "args": argv[1:]}}})
        return 0
    from . import mcp_server
    if args.transport == "http":
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        mcp_server.serve_http(args.home, args.profile, args.client, args.port)
    else:
        mcp_server.serve_stdio(args.home, args.profile, args.client)
    return 0


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
            report = _doctor(args.home)
            _emit(report)
            return 0 if report["ready"] else 2
        elif args.command == "graph":
            _emit(_contracts())
        elif args.command == "ui":
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            if args.control:
                from .dashboard import serve_centre
                serve_centre(args.home, args.port, args.workers)
            else:
                from .dashboard import serve_dashboard
                serve_dashboard(args.home, args.port)
        elif args.command in ("repo", "ticket", "run", "models", "mcp"):
            return _platform_command(args)
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
