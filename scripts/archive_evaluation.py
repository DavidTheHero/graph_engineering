"""Retain portable raw receipts while excluding large runtime Git trees and profiles."""
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.runtime import Store, redact

root = Path(__file__).resolve().parents[1]
count = 0
for case in ("run-01", "run-03"):
    for mode in ("multimodel", "baseline"):
        home = root / "evidence/evaluation" / case / mode / "controller"
        store = Store(home)
        for run in store.list_runs():
            target = root / "evidence/final/evaluation" / case / mode / run["id"]
            target.mkdir(parents=True, exist_ok=True)
            (target / "run.json").write_text(json.dumps(redact(run), indent=2), encoding="utf-8")
            events = store.events(run["id"])
            (target / "events.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
            worker_root = home / "workers" / run["id"]
            for result in worker_root.glob("*/result.json"):
                destination = target / "workers" / result.parent.name
                destination.mkdir(parents=True, exist_ok=True)
                for name in ("result.json", "input.json", "opencode.json"):
                    if (result.parent / name).exists():
                        shutil.copy2(result.parent / name, destination / name)
            count += 1
        store.close()
print(json.dumps({"archived_run_receipts": count, "directory": str(root / 'evidence/final/evaluation')}))
