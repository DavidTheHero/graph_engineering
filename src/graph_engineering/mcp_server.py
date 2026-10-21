"""MCP tools for other agents: file tickets and follow them.

Accepting a ticket, publishing, approving a merge and cancelling are a person's decisions, so they are
not exposed here. Needs the optional ``mcp`` package (``pip install 'graph-engineering[mcp]'``).
"""
from __future__ import annotations

from .repos import Repos
from .store import Store

TICKET_FIELDS = ("id", "repo", "state", "round", "max_rounds", "error", "working", "created", "updated")


def summary(ticket: dict) -> dict:
    data = ticket["data"]
    return {**{k: ticket[k] for k in TICKET_FIELDS}, "spec": ticket["spec"], "pr": data.get("pr"),
            "merge_commit": data.get("merge_commit"), "rounds": [
                {"round": r["round"], "coding": r["coding"]["status"], "gates_passed": all(g["ok"] for g in r.get("gates", [])),
                 "review": (r.get("review") or {}).get("verdict")} for r in data.get("rounds", [])]}


def tools(store: Store, repos: Repos) -> dict:
    def list_repositories() -> list[dict]:
        """Repositories this controller can work on."""
        return [{"name": r.name, "github": r.slug, "base": r.base} for r in repos.all()]

    def create_ticket(repo: str, request: str) -> dict:
        """File a request in plain words. An intake agent turns it into a ticket; a person accepts it before development."""
        target = repos.get(repo)
        if not request.strip():
            raise ValueError("request is empty")
        return summary(store.create(target.name, request.strip()[:20000], "mcp", target.max_rounds))

    def list_tickets(state: str | None = None, repo: str | None = None) -> list[dict]:
        """Tickets, newest activity first, optionally filtered by state or repository."""
        return [summary(t) for t in store.list(state, repo, limit=100)]

    def get_ticket(ticket_id: str) -> dict:
        """One ticket with its rounds, PR and the last workflow events."""
        ticket = store.get(ticket_id)
        if ticket is None:
            raise KeyError(ticket_id)
        events = [{"kind": e["kind"], "actor": e["actor"], "at": e["at"], "data": e["data"]}
                  for e in store.events(ticket_id) if e["kind"] != "agent"][-30:]
        return {**summary(ticket), "request": ticket["request"], "events": events}

    def ticket_activity(ticket_id: str, after: int = 0) -> list[dict]:
        """Agent steps for a ticket after event number ``after`` (to follow progress)."""
        return [{"seq": e["seq"], "actor": e["actor"], **e["data"]} for e in store.events(ticket_id, after)
                if e["kind"] == "agent"][-200:]

    return {f.__name__: f for f in (list_repositories, create_ticket, list_tickets, get_ticket, ticket_activity)}


def serve_stdio(store: Store, repos: Repos) -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("the MCP server needs the optional package: pip install 'graph-engineering[mcp]'") from exc
    server = FastMCP("graph_engineering")
    for function in tools(store, repos).values():
        server.tool()(function)
    server.run()
