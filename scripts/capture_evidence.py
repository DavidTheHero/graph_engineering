"""Refresh read-only evidence, portable templates and complete final receipts."""
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from graph_engineering.models import default_models, generate_opencode_config
from graph_engineering.workflow import graph_contracts, PATCH_SCHEMA
from graph_engineering.runtime import redact, Store


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(value), indent=2, ensure_ascii=False), encoding="utf-8")


commands = {
    "git": ["git", "--version"], "node": ["node", "--version"], "github_cli": ["gh", "--version"],
    "opencode": [str(Path(os.environ["APPDATA"]) / "npm/node_modules/@opencode/cli/bin/opencode.exe"), "--version"],
}
environment = {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version, "tools": {}}
for name, cmd in commands.items():
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    environment["tools"][name] = {"argv": cmd, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
write(root / "evidence/environment/observed.json", environment)
write(root / "docs/node-contracts.json", graph_contracts())
write(root / "configs/opencode-v2-implementation.example.json", generate_opencode_config(default_models()["a"], "implementation", PATCH_SCHEMA))
write(root / "configs/models.example.json", default_models())
write(root / "configs/repository.example.json", {"version": 1, "repo": "D:/path/to/repository", "base": "main", "backend": "local", "slug": None,
    "trusted_local": False, "auto_merge": False, "auto_merge_paths": [], "allow_low_confidence": False,
    "test_command": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"], "build_commands": [],
    "max_repairs": 2, "max_node_attempts": 2, "command_timeout": 120,
    "models": default_models(), "routing": "baseline", "required_checks": [], "allow_same_model_review": True, "protected_paths": []})

slug = "DavidTheHero/graph-engineering-public-fixture-20261018"
receipts = {
    "pr": ["gh", "api", f"repos/{slug}/pulls/2"],
    "pr-diff": ["gh", "pr", "diff", "2", "--repo", slug],
    "reviews": ["gh", "api", f"repos/{slug}/pulls/2/reviews"],
    "issues": ["gh", "issue", "list", "--repo", slug, "--state", "all", "--json", "number,title,body,state"],
    "protection": ["gh", "api", f"repos/{slug}/branches/main/protection"],
    "rules": ["gh", "api", f"repos/{slug}/rules/branches/main"],
    "actions": ["gh", "run", "list", "--repo", slug, "--limit", "10", "--json", "databaseId,status,conclusion,headSha,event,url"],
    "ci-failure-log": ["gh", "run", "view", "39156746520", "--repo", slug, "--log-failed"],
    "ci-pr-log": ["gh", "run", "view", "39156848318", "--repo", slug, "--log"],
    "ci-postmerge-log": ["gh", "run", "view", "39157033913", "--repo", slug, "--log"],
}
for name, cmd in receipts.items():
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    write(root / "evidence/final/live" / (name + ".json"), {"argv": cmd, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
for name, controller in [("public", "evidence/live/public-01/controller"), ("private", "evidence/live/run-01/controller")]:
    store = Store(root / controller)
    run = store.list_runs()[0]
    write(root / "evidence/final" / (name + "-run.json"), run)
    write(root / "evidence/final" / (name + "-events.json"), store.events(run["id"]))
    store.close()

original = root / "evidence/live/public-01/recovery"
store = Store(original)
write(root / "evidence/final/recovery-original-failure.json", {
    "observed": json.loads((original / "child-observed.json").read_text()),
    "effects": [dict(r) for r in store.db.execute("SELECT * FROM effects")],
    "outcome": "Original implementation created duplicate #4 after crash creating #3; both closed. Corrected read-only recovery retained in separate receipt."})
store.close()
write(root / "evidence/final/recovery-corrected.json", json.loads((original / "corrected-01/receipt.json").read_text()))
print("Captured environment, graph/contracts, configuration templates and actual live GitHub receipts")
