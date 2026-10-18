"""Actual crash/reconciliation test in the authorised public disposable repository."""
import json
import os
from pathlib import Path
import subprocess
import sys
import re
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.publication import GitHubBackend
from graph_engineering.runtime import Store

case = sys.argv[sys.argv.index("--case") + 1] if "--case" in sys.argv else "corrected"
if not re.fullmatch(r"[a-z0-9-]{1,30}", case): raise ValueError("Invalid case")
root = Path(__file__).resolve().parents[1] / "evidence/live/public-01/recovery" / case
repo = root.parent.parent / "python-invoice"
slug = "DavidTheHero/graph-engineering-public-fixture-20261018"
key = "controller-crash-recovery-20261018-" + case
root.mkdir(parents=True, exist_ok=True)
if "--child" in sys.argv:
    store = Store(root)
    if not store.db.execute("SELECT id FROM runs WHERE id='crash_recovery'").fetchone():
        store.create_run({"repo": str(repo)}, "crash_recovery")
    backend = GitHubBackend(root / "backend", repo, slug, "main")
    def crash_after_remote_write():
        issue = backend.ensure_issue(key, "Controller failure-injection: reconcile interrupted issue creation",
            "Explicit infrastructure test: the controller exits after observing this real issue and before recording its local result. A resumed controller must find this marker and create no duplicate.")
        (root / "child-observed.json").write_text(json.dumps(issue, indent=2))
        os._exit(23)  # Deliberately skip finally/SQLite result write.
    store.effect("crash_recovery", "issue", crash_after_remote_write)
    raise SystemExit("Expected injected crash did not occur")

child = subprocess.run([sys.executable, __file__, "--case", case, "--child"], capture_output=True, text=True, timeout=90)
if child.returncode != 23:
    raise RuntimeError(f"Crash child exited unexpectedly: {child.returncode}: {child.stderr}")
store = Store(root)
intent = dict(store.db.execute("SELECT * FROM effects WHERE run_id='crash_recovery' AND key='issue'").fetchone())
if intent["status"] != "intent": raise RuntimeError("Crash did not leave a durable intent")
backend = GitHubBackend(root / "backend", repo, slug, "main")
observed = json.loads((root / "child-observed.json").read_text())
for attempt in range(5):
    try:
        reconciled = store.effect("crash_recovery", "issue", lambda: (_ for _ in ()).throw(RuntimeError("Creation during recovery is forbidden")),
                                 on_uncertain=lambda: backend.reconcile_issue(key))
        break
    except RuntimeError:
        if attempt == 4: raise
        time.sleep(2)
matches = backend._matches("issues", key)
if reconciled["number"] != observed["number"] or len(matches) != 1:
    raise RuntimeError("Duplicate or inconsistent issue after actual controller death")
closed = backend.update_issue(reconciled["number"], state="closed")
receipt = {"child_exit_code": child.returncode, "intent_after_crash": intent, "observed_before_crash": observed,
           "reconciled_after_restart": reconciled, "matching_remote_issues": len(matches), "duplicate_issues": len(matches)-1,
           "closed": closed["state"] == "closed", "backend": "LIVE GitHub"}
(root / "receipt.json").write_text(json.dumps(receipt, indent=2))
store.close()
print(json.dumps(receipt, indent=2))
