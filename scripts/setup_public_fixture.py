"""Configure and run only the expressly authorised disposable public fixture."""
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.workflow import initialise, Workflow

root = Path(__file__).resolve().parents[1] / "evidence/live/public-01"
slug = "DavidTheHero/graph-engineering-public-fixture-20261018"
policy = {"required_status_checks": {"strict": True, "contexts": ["regression"]}, "enforce_admins": True,
          "required_pull_request_reviews": None, "restrictions": None, "allow_force_pushes": False, "allow_deletions": False}
result = subprocess.run(["gh", "api", f"repos/{slug}/branches/main/protection", "--method", "PUT", "--input", "-"],
                        input=json.dumps(policy), text=True, capture_output=True, timeout=30)
(root / "protection-receipt.json").write_text(json.dumps({"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, indent=2))
if result.returncode: raise SystemExit(result.stderr)
flow = Workflow(root / "controller")
config = initialise(root / "python-invoice", flow.root, backend="github", slug=slug, trusted_local=True)
run = flow.new_run(config)
(root / "run-id.txt").write_text(run["id"])
run = flow.execute(run["id"])
(root / "run-result.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
print(json.dumps({"id": run["id"], "status": run["status"], "node": run["node"], "pr": run["state"].get("pr"), "head": run["state"].get("head"), "error": run["state"].get("error"), "gate": run["state"].get("gate")}, indent=2))
flow.store.close()
