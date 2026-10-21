"""Durable state: tickets, an append-only event log, and agent runs, in one SQLite file."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import sqlite3
import time

# state -> states it may move to. Anything else is refused.
TRANSITIONS = {
    "new": {"proposed", "needs_info", "rejected", "failed", "cancelled"},
    "needs_info": {"new", "rejected", "cancelled"},
    "proposed": {"accepted", "rejected", "new", "cancelled"},
    "accepted": {"accepted", "ready", "failed", "cancelled"},
    "ready": {"awaiting_merge", "accepted", "cancelled"},
    "awaiting_merge": {"merging", "accepted", "cancelled"},
    "merging": {"merging", "done", "awaiting_merge", "cancelled"},
    "failed": {"new", "accepted", "cancelled"},
    "rejected": {"new"},
    "done": set(),
    "cancelled": set(),
}
WORK = ("new", "accepted", "ready", "merging")       # states a worker advances
WAITING = ("needs_info", "proposed", "awaiting_merge")  # states waiting for a person
FINAL = ("done", "rejected", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL,
  spec TEXT NOT NULL DEFAULT '{}', data TEXT NOT NULL DEFAULT '{}', round INTEGER NOT NULL DEFAULT 0,
  max_rounds INTEGER NOT NULL DEFAULT 3, error TEXT, lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
  not_before REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
  actor TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_ticket ON events(ticket, seq);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, ticket TEXT NOT NULL, role TEXT NOT NULL, model TEXT NOT NULL, round INTEGER NOT NULL,
  started REAL NOT NULL, ended REAL, status TEXT, dir TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '{}');
"""


class Conflict(RuntimeError):
    """The ticket is not in a state that allows this; nothing was changed."""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _ticket(row) -> dict | None:
        if row is None:
            return None
        ticket = dict(row)
        ticket["spec"], ticket["data"] = json.loads(ticket["spec"]), json.loads(ticket["data"])
        ticket["working"] = ticket["lease_until"] > time.time()
        return ticket

    def _event(self, db, ticket: str, kind: str, actor: str, data: dict | None) -> None:
        db.execute("INSERT INTO events (ticket, at, kind, actor, data) VALUES (?, ?, ?, ?, ?)",
                   (ticket, time.time(), kind, actor, json.dumps(data or {})))

    def create(self, repo: str, request: str, actor: str, max_rounds: int = 3, spec: dict | None = None,
               state: str = "new") -> dict:
        if state not in ("new", "proposed"):
            raise ValueError("a ticket starts as new or proposed")
        ticket = "GE-" + secrets.token_hex(4).upper()
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO tickets (id, repo, request, state, spec, max_rounds, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (ticket, repo, request, state, json.dumps(spec or {}), max_rounds, now, now))
            self._event(db, ticket, "created", actor, {"repo": repo, "request": request, "state": state})
            db.execute("COMMIT")
        return self.get(ticket)

    def get(self, ticket: str) -> dict | None:
        with self.connect() as db:
            return self._ticket(db.execute("SELECT * FROM tickets WHERE id = ?", (ticket,)).fetchone())

    def list(self, state: str | None = None, repo: str | None = None, limit: int = 200) -> list[dict]:
        query, args = "SELECT * FROM tickets WHERE 1=1", []
        if state:
            query, args = query + " AND state = ?", args + [state]
        if repo:
            query, args = query + " AND repo = ?", args + [repo]
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY updated DESC LIMIT ?", args + [limit]).fetchall()
        return [self._ticket(r) for r in rows]

    def transition(self, ticket: str, to: str, actor: str, kind: str, event: dict | None = None, *,
                   expect: tuple | None = None, owner: str | None = None, **fields) -> dict:
        """Move a ticket to ``to`` if its state is in ``expect`` (default: any state allowed to reach ``to``).

        ``owner`` must hold the lease when a worker moves a ticket; a person's action requires no live lease
        except for cancellation, which is always allowed."""
        allowed = {"spec", "data", "round", "max_rounds", "error", "not_before"}
        if set(fields) - allowed:
            raise ValueError(f"unknown fields {set(fields) - allowed}")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise KeyError(ticket)
            state = row["state"]
            if to not in TRANSITIONS[state] or (expect and state not in expect):
                db.execute("ROLLBACK")
                raise Conflict(f"{ticket} is {state}; cannot move to {to}")
            leased = row["lease_until"] > time.time()
            if owner is not None and row["lease_owner"] != owner:
                db.execute("ROLLBACK")
                raise Conflict(f"{ticket} lease is not held by {owner}")
            if owner is None and leased and to != "cancelled":
                db.execute("ROLLBACK")
                raise Conflict(f"{ticket} is being worked on; wait for the current step or cancel it")
            values = {k: json.dumps(v) if k in ("spec", "data") else v for k, v in fields.items()}
            values.update(state=to, updated=time.time(), lease_owner=None, lease_until=0)  # a step ends with its transition
            db.execute(f"UPDATE tickets SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?", [*values.values(), ticket])
            self._event(db, ticket, kind, actor, {"from": state, "to": to, **(event or {})})
            db.execute("COMMIT")
        return self.get(ticket)

    def merge_data(self, ticket: str, **values) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tickets WHERE id = ?", (ticket,)).fetchone()
            data = {**json.loads(row["data"]), **values}
            db.execute("UPDATE tickets SET data = ?, updated = ? WHERE id = ?", (json.dumps(data), time.time(), ticket))
            db.execute("COMMIT")
        return data

    def event(self, ticket: str, kind: str, actor: str, data: dict | None = None) -> None:
        with self.connect() as db:
            self._event(db, ticket, kind, actor, data)

    def events(self, ticket: str, after: int = 0, limit: int = 2000) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE ticket = ? AND seq > ? ORDER BY seq LIMIT ?", (ticket, after, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def claim(self, owner: str, ttl: float, states=WORK, skip=()) -> dict | None:
        """Lease the oldest ticket that is due in one of ``states``."""
        now = time.time()
        marks = ",".join("?" * len(states))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(f"SELECT id, state, data FROM tickets WHERE state IN ({marks}) AND lease_until < ? AND not_before <= ? "
                              "ORDER BY updated", (*states, now, now)).fetchall()
            row = next((r for r in rows if r["id"] not in skip and self._due(r)), None)
            if row is None:
                db.execute("ROLLBACK")
                return None
            db.execute("UPDATE tickets SET lease_owner = ?, lease_until = ? WHERE id = ?", (owner, now + ttl, row["id"]))
            db.execute("COMMIT")
        return self.get(row["id"])

    @staticmethod
    def _due(row) -> bool:
        # A ready ticket waits for a person unless its repository publishes automatically (recorded at acceptance).
        return row["state"] != "ready" or json.loads(row["data"]).get("publish") is True

    def renew(self, ticket: str, owner: str, ttl: float) -> bool:
        with self.connect() as db:
            cursor = db.execute("UPDATE tickets SET lease_until = ? WHERE id = ? AND lease_owner = ?", (time.time() + ttl, ticket, owner))
            return cursor.rowcount == 1

    def release(self, ticket: str, owner: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as db:
            db.execute(f"UPDATE tickets SET lease_owner = NULL, lease_until = 0{', ' + sets if sets else ''} WHERE id = ? AND lease_owner = ?",
                       (*fields.values(), ticket, owner))

    def run_start(self, ticket: str, role: str, model: str, round_: int, directory: Path) -> str:
        run = secrets.token_hex(6)
        with self.connect() as db:
            db.execute("INSERT INTO runs (id, ticket, role, model, round, started, dir) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (run, ticket, role, model, round_, time.time(), str(directory)))
        return run

    def run_end(self, run: str, status: str, usage: dict) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET ended = ?, status = ?, usage = ? WHERE id = ?", (time.time(), status, json.dumps(usage), run))

    def runs(self, ticket: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE ticket = ? ORDER BY started", (ticket,)).fetchall()
        return [{**dict(r), "usage": json.loads(r["usage"])} for r in rows]

    def run(self, run: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run,)).fetchone()
        return dict(row) if row else None
