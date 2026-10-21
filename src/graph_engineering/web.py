"""The control centre: a JSON API and one page, on localhost only.

Every write needs the ``X-GE`` header (a cross-site form cannot send it) and a matching Origin, and the
Host header must be this server, so another site cannot drive it through the browser.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from . import gates, workflow
from .store import Conflict

PAGE = Path(__file__).parent / "web" / "index.html"
ACTIONS = {"accept", "reject", "answer", "approve-publish", "approve-merge", "request-changes", "retry", "cancel"}


def ticket_view(ctx, ticket_id: str) -> dict:
    ticket = ctx.store.get(ticket_id)
    if ticket is None:
        raise KeyError(ticket_id)
    events = [e for e in ctx.store.events(ticket_id) if e["kind"] != "agent"]
    return {**ticket, "events": events, "runs": ctx.store.runs(ticket_id)}


def ticket_diff(ctx, ticket: dict) -> str:
    work = ctx.ticket_dir(ticket["id"]) / "work"
    base = ticket["data"].get("base")
    if not base or not (work / ".git").exists() or ticket["working"]:
        return ""
    return gates.diff(work, base)


def transcript(ctx, run_id: str, limit: int = 400) -> list[dict]:
    run = ctx.store.run(run_id)
    if run is None:
        raise KeyError(run_id)
    path = Path(run["dir"]) / "transcript.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines()[-limit:]:
        entry = json.loads(line)
        for key in ("content", "reasoning"):
            if isinstance(entry.get(key), str) and len(entry[key]) > 6000:
                entry[key] = entry[key][:6000] + f"\n[... {len(entry[key]) - 6000} more characters]"
        rows.append(entry)
    return rows


def act(ctx, ticket_id: str, action: str, body: dict, who: str) -> dict:
    store, repos, text = ctx.store, ctx.repos, str(body.get("text") or "")
    if action == "approve-merge":
        return workflow.approve_merge(store, ticket_id, who, str(body.get("head") or ""))
    return {"accept": lambda: workflow.accept(store, repos, ticket_id, who, body.get("spec")),
            "reject": lambda: workflow.reject(store, ticket_id, who, text),
            "answer": lambda: workflow.answer(store, ticket_id, who, text),
            "approve-publish": lambda: workflow.approve_publish(store, ticket_id, who),
            "request-changes": lambda: workflow.request_changes(store, ticket_id, who, text),
            "retry": lambda: workflow.retry(store, ticket_id, who),
            "cancel": lambda: workflow.cancel(store, ticket_id, who, text)}[action]()


def make_handler(ctx, scheduler, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status: int, payload, content_type: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def guard(self, write: bool) -> bool:
            if self.headers.get("Host") not in hosts:
                self.send(421, {"error": "unexpected Host"})
                return False
            if write:
                origin = self.headers.get("Origin")
                if self.headers.get("X-GE") != "1" or (origin and urlparse(origin).netloc not in hosts):
                    self.send(403, {"error": "cross-site request refused"})
                    return False
            return True

        def do_GET(self):
            if not self.guard(False):
                return
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/state":
                    return self.send(200, {
                        "tickets": [{k: t[k] for k in ("id", "repo", "state", "round", "max_rounds", "error", "working", "updated",
                                                       "created")} | {"title": t["spec"].get("title") or t["request"][:90]}
                                    for t in ctx.store.list()],
                        "repos": [{"name": r.name, "slug": r.slug, "auto_accept": r.auto_accept, "auto_publish": r.auto_publish}
                                  for r in ctx.repos.all()],
                        "workers": list(scheduler.status.values()) if scheduler else [],
                        "models": ctx.registry.describe()})
                match = re.fullmatch(r"/api/tickets/(GE-[0-9A-F]{8})(/events|/diff)?", url.path)
                if match:
                    ticket_id, part = match.groups()
                    if part == "/events":
                        after = int(parse_qs(url.query).get("after", ["0"])[0])
                        return self.send(200, ctx.store.events(ticket_id, after))
                    if part == "/diff":
                        return self.send(200, {"diff": ticket_diff(ctx, ctx.store.get(ticket_id))})
                    return self.send(200, ticket_view(ctx, ticket_id))
                match = re.fullmatch(r"/api/runs/([0-9a-f]{12})/transcript", url.path)
                if match:
                    return self.send(200, transcript(ctx, match.group(1)))
                self.send(404, {"error": "not found"})
            except KeyError:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.guard(True):
                return
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 200_000:
                    return self.send(413, {"error": "request too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                who = "person:control-centre"
                if url.path == "/api/tickets":
                    repo = ctx.repos.get(str(body.get("repo")))
                    request = str(body.get("request") or "").strip()
                    if not request:
                        return self.send(400, {"error": "describe what you want"})
                    return self.send(201, ctx.store.create(repo.name, request, who, repo.max_rounds))
                match = re.fullmatch(r"/api/tickets/(GE-[0-9A-F]{8})/([a-z-]+)", url.path)
                if match and match.group(2) in ACTIONS:
                    return self.send(200, act(ctx, match.group(1), match.group(2), body, who))
                self.send(404, {"error": "not found"})
            except Conflict as exc:
                self.send(409, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                self.send(400, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(ctx, scheduler=None, port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ctx, scheduler, port))
    print(f"control centre: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
