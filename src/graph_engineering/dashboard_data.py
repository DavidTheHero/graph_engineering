"""Read-only projections of observed controller state for the local dashboard."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3

from .runtime import redact
from .workflow import graph_contracts


EVIDENCE = ("candidate", "investigation", "base_sha", "baseline", "validation", "priority", "plan",
            "branch", "head", "verification", "diff", "review", "issue", "pr",
            "policy_snapshot", "gate", "merge", "postmerge", "outcome", "error", "repairs")
ROLE_NODES = {"investigation": "issue_detection", "planning": "task_planning",
              "implementation": "isolated_implementation", "review": "independent_review"}
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def bounded(value, depth=0):
    """Exclude configuration/credentials and bound untrusted display payloads."""
    if depth > 10:
        return "[truncated depth]"
    if isinstance(value, str):
        return value[:24000] + ("\n[truncated for display]" if len(value) > 24000 else "")
    if isinstance(value, dict):
        return {str(k): bounded(v, depth + 1) for k, v in list(value.items())[:100]
                if k != "config" and not re.search(r"(?:token|secret|password|api_key|authorization)", str(k), re.I)}
    if isinstance(value, list):
        return [bounded(v, depth + 1) for v in value[:100]]
    return value


def summary(run, source):
    state = run.get("state", {})
    config = state.get("config", {})
    error = state.get("error")
    attention = error.get("message", str(error)) if isinstance(error, dict) else error
    # Recovered failures can remain in state; only surface errors on an unresolved run.
    if run["status"] not in ("blocked", "failed", "escalated", "cancelled"):
        attention = None
    if run["status"] == "waiting_approval":
        attention = "Human approval required for the tested commit."
    if not attention and run["status"] == "blocked":
        attention = "; ".join(state.get("gate", {}).get("reasons", [])) or "This run requires supervisor attention."
    repo = run.get("repo", config.get("repo", "Unknown repository"))
    duration = None
    try:
        duration = max(0, (datetime.fromisoformat(run["updated"]) - datetime.fromisoformat(run["created"])).total_seconds())
    except (KeyError, ValueError, TypeError):
        pass
    return bounded({"id": run["id"], "source": source,
        "source_kind": "live" if source == "live" else "recorded",
        "title": state.get("candidate", {}).get("title") or "Repository inspection",
        "repo": repo, "repo_name": config.get("slug") or re.split(r"[/\\]", repo.rstrip("/\\"))[-1],
        "status": run["status"], "node": run["node"], "backend": config.get("backend", "unknown"),
        "routing": config.get("routing", "unknown"), "created": run.get("created"),
        "updated": run.get("updated"), "duration_seconds": duration,
        "cancel_requested": bool(run.get("cancel")), "attention": attention,
        "head": state.get("head"), "branch": state.get("branch"),
        "issue": state.get("issue"), "pr": state.get("pr"), "repairs": state.get("repairs", 0)})


def node_states(run, events):
    nodes = {c["name"]: {"status": "not_run", "attempts": 0, "failures": 0, "last_at": None}
             for c in graph_contracts()}
    for event in events:
        node = nodes.get(event["node"])
        if node is None:
            continue
        kind, data = event["kind"], event.get("data", {})
        node["last_at"] = event.get("at")
        if kind == "started":
            node["attempts"] += 1
            node["status"] = "running"
        elif kind in ("failed", "escalated", "exhausted"):
            node["failures"] += 1
            node["status"] = "failed"
        elif kind == "completed":
            updates = data.get("updates", {})
            node["status"] = "completed"
            if data.get("status") in ("blocked", "waiting_approval", "escalated"):
                node["status"] = data["status"]
            if (updates.get("verification", {}).get("passed") is False
                    or updates.get("review", {}).get("approved") is False):
                node["status"] = "needs_repair"
        elif kind == "cancelled":
            node["status"] = "cancelled"
    cursor = run["node"]
    if cursor in nodes:
        nodes[cursor]["status"] = run["status"]
    return nodes


class Catalog:
    """Only the operator-selected DB and fixed archive entries are addressable."""

    def __init__(self, home: Path, archive: Path | None = None):
        self.home = Path(home).resolve()
        self.archive = Path(archive).resolve() if archive else None

    def _connect(self):
        db = sqlite3.connect((self.home / "state.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] != 1:
                raise ValueError("Unsupported controller database version")
        except Exception:
            db.close()
            raise
        return db

    @staticmethod
    def _decode(row):
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    def _recordings(self):
        if not self.archive or not self.archive.is_dir():
            return []
        pairs = [(self.archive / f"{name}-run.json", self.archive / f"{name}-events.json")
                 for name in ("public", "private")]
        pairs.extend((p, p.with_name("events.json")) for p in sorted(
            (self.archive / "evaluation" / "run-03").glob("*/*/run.json")))
        result = []
        for path, event_path in pairs:
            if path.is_file() and event_path.is_file():
                run = json.loads(path.read_text(encoding="utf-8-sig"))
                result.append((run, event_path))
        return result

    def overview(self):
        runs = []
        if (self.home / "state.sqlite3").is_file():
            with closing(self._connect()) as db:
                runs.extend(summary(self._decode(row), "live") for row in db.execute(
                    "SELECT * FROM runs ORDER BY created DESC LIMIT 200"))
        recordings = self._recordings()
        runs.extend(summary(run, "recorded") for run, _ in recordings)
        return redact({"runs": runs, "contracts": graph_contracts(),
            "sources": [{"id": "live", "label": "Live controller"},
                        *([{"id": "recorded", "label": "Recorded demonstrations"}] if recordings else [])],
            "limits": {"live_runs": 200, "events_per_run": 2000, "text_characters": 24000},
            "read_only": True})

    def detail(self, source, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise KeyError("Unknown run")
        approval = None
        if source == "live" and (self.home / "state.sqlite3").is_file():
            with closing(self._connect()) as db:
                row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError("Unknown run")
                run = self._decode(row)
                # Stream the full history to derive truthful node states without
                # retaining unbounded events in memory or sending them to the UI.
                nodes = node_states(run, (dict(r) | {"data": json.loads(r["data"])} for r in db.execute(
                    "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))))
                # Oldest first; bounded response includes an explicit truncation flag.
                rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq LIMIT 2001", (run_id,)).fetchall()
                events = [dict(r) | {"data": json.loads(r["data"])} for r in rows]
                row = db.execute("SELECT head,actor,at FROM approvals WHERE run_id=?", (run_id,)).fetchone()
                approval = dict(row) if row else None
        elif source == "recorded":
            pair = next(((r, p) for r, p in self._recordings() if r["id"] == run_id), None)
            if pair is None:
                raise KeyError("Unknown run")
            run, path = pair
            events = json.loads(path.read_text(encoding="utf-8-sig"))
            nodes = node_states(run, events)
            approval_event = next((e for e in reversed(events) if e["kind"] == "human_approval"), None)
            if approval_event:
                approval = approval_event["data"] | {"at": approval_event.get("at")}
        else:
            raise KeyError("Unknown source")
        truncated = len(events) > 2000
        events = events[:2000]
        calls = [e["data"] | {"at": e.get("at"), "node": e['data'].get('graph_node') or ROLE_NODES.get(e["data"].get("role"), e["node"])}
                 for e in events if e["kind"] == "model_call"]
        state = run.get("state", {})
        return redact({"run": summary(run, source), "nodes": nodes,
            "events": [bounded(e) for e in events], "events_truncated": truncated,
            "evidence": bounded({k: state[k] for k in EVIDENCE if k in state}),
            "model_calls": [bounded(c) for c in calls], "approval": bounded(approval)})
