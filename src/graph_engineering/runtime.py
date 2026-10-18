"""Single-host durability. Transactions contain state and the matching audit event."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
import uuid
import time


NODE_DEADLINE = ContextVar("graph_node_deadline", default=None)


def remaining_timeout(requested: float) -> float:
    deadline = NODE_DEADLINE.get()
    if deadline is None:
        return requested
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Graph node deadline exhausted")
    return min(requested, remaining)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def redact(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    for key, secret in os.environ.items():
        if len(secret) > 7 and any(s in key.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
    text = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", text)
    return json.loads(text)


class BusyError(RuntimeError):
    pass


class RepoLock:
    """Kernel releases the lock on controller death; no time-expiry split brain."""

    def __init__(self, root: Path, repo_key: str):
        path = Path(root) / "locks" / (digest(repo_key) + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        if not os.fstat(self.file.fileno()).st_size:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise BusyError("Another controller owns this repository") from exc
        return self

    def __exit__(self, *_):
        if self.file is not None:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_version VALUES(1);
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, repo TEXT NOT NULL, node TEXT NOT NULL,
                status TEXT NOT NULL, state TEXT NOT NULL, cancel INTEGER DEFAULT 0,
                created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                at TEXT NOT NULL, node TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS effects(
                run_id TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, PRIMARY KEY(run_id,key));
            CREATE TABLE IF NOT EXISTS findings(
                fingerprint TEXT PRIMARY KEY, run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals(
                run_id TEXT PRIMARY KEY, head TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL);
        """)
        version = self.db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version != 1:
            raise RuntimeError(f"Unsupported database version: {version}")

    def close(self):
        self.db.close()

    def create_run(self, config: dict, run_id: str | None = None) -> dict:
        run_id = uuid.uuid4().hex[:16] if run_id is None else run_id
        self._validate_run_id(run_id)
        state = {"config": redact(config), "task_id": run_id, "attempts": {}, "repairs": 0}
        with self.db:
            self.db.execute("INSERT INTO runs(id,repo,node,status,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                            (run_id, config["repo"], "repository_discovery", "ready", json.dumps(state), now(), now()))
            self._event(run_id, "repository_discovery", "created", {"config": config})
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    def list_runs(self) -> list[dict]:
        return [self.get(r[0]) for r in self.db.execute("SELECT id FROM runs ORDER BY created")]

    def _event(self, run_id: str, node: str, kind: str, data: dict):
        self.db.execute("INSERT INTO events(run_id,at,node,kind,data) VALUES(?,?,?,?,?)",
                        (run_id, now(), node, kind, json.dumps(redact(data))))

    def event(self, run_id: str, node: str, kind: str, data: dict):
        with self.db:
            self._event(run_id, node, kind, data)

    def checkpoint(self, run_id: str, node: str, updates: dict, next_node: str,
                   status: str = "ready", kind: str = "completed"):
        # Caller owns the OS repository lock. Merge rather than overwrite cancellation.
        state = self.get(run_id)["state"]
        state.update(redact(updates))
        with self.db:
            self.db.execute("UPDATE runs SET node=?,status=?,state=?,updated=? WHERE id=?",
                            (next_node, status, json.dumps(state), now(), run_id))
            self._event(run_id, node, kind, {"next": next_node, "status": status, "updates": updates})

    def events(self, run_id: str) -> list[dict]:
        return [dict(r) | {"data": json.loads(r["data"])} for r in self.db.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]

    def artifact(self, run_id: str, name: str, content: Any) -> str:
        self._validate_run_id(run_id)
        directory = self.root / "runs" / run_id
        path = directory / name
        if (path.parent != directory or not name.endswith(".json")
                or self.root not in directory.resolve().parents):
            raise ValueError("Unsafe artifact name")
        directory.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(redact(content), indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        return str(path)

    @staticmethod
    def _validate_run_id(run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise ValueError("Run ID must contain 1-64 letters, digits, underscores or hyphens")

    def effect(self, run_id: str, key: str, reconcile: Callable[[], dict],
               on_uncertain: Callable[[], dict] | None = None) -> dict:
        row = self.db.execute("SELECT status,result FROM effects WHERE run_id=? AND key=?", (run_id, key)).fetchone()
        if row and row["status"] == "done":
            return json.loads(row["result"])
        if row and on_uncertain is None:
            raise RuntimeError(f"External effect '{key}' has uncertain outcome; read-only reconciliation required")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO effects VALUES(?,?,?,NULL)", (run_id, key, "intent"))
        # An absent list result does NOT prove that a POST was never applied.
        result = on_uncertain() if row else reconcile()
        with self.db:
            self.db.execute("UPDATE effects SET status='done',result=? WHERE run_id=? AND key=?",
                            (json.dumps(redact(result)), run_id, key))
        return result

    def claim_finding(self, fingerprint: str, run_id: str) -> str:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO findings VALUES(?,?)", (fingerprint, run_id))
        return self.db.execute("SELECT run_id FROM findings WHERE fingerprint=?", (fingerprint,)).fetchone()[0]

    def stop(self, run_id: str):
        with self.db:
            self.db.execute("UPDATE runs SET cancel=1 WHERE id=?", (run_id,))
            self._event(run_id, "controller", "cancel_requested", {})

    def clear_stop(self, run_id: str):
        run = self.get(run_id)
        state = run["state"]
        state.pop("error", None)
        key = f"{run['node']}:{state.get('repairs', 0)}"
        previous = state.get("attempts", {}).get(key, 0)
        state.setdefault("attempts", {})[key] = 0
        with self.db:
            self.db.execute("UPDATE runs SET cancel=0,state=? WHERE id=?", (json.dumps(state), run_id))
            self._event(run_id, run["node"], "resume_requested", {"prior_attempts": previous})

    def approve(self, run_id: str, head: str, actor: str):
        run = self.get(run_id)
        if run["status"] != "waiting_approval" or run["state"].get("head") != head:
            raise ValueError("Approval requires waiting_approval and the exact tested head SHA")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?)", (run_id, head, actor, now()))
            self._event(run_id, "approval_gate", "human_approval", {"head": head, "actor": actor})

    def approved_head(self, run_id: str) -> str | None:
        row = self.db.execute("SELECT head FROM approvals WHERE run_id=?", (run_id,)).fetchone()
        return row[0] if row else None
