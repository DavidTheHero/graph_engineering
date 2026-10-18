"""Actual OpenCode read-tool capability probe; production workers deny tools."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.models import default_models, detect_opencode, generate_opencode_config, sanitized_environment, _run_opencode

parser = argparse.ArgumentParser()
parser.add_argument("--model", choices=["a", "b"], required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.output.resolve()
cwd = root / "cwd"
cwd.mkdir(parents=True, exist_ok=True)
(cwd / "fixture.txt").write_text("FAIL test_add: expected add(2,3)==5; actual 6\n", encoding="utf-8")
model = default_models()[args.model]
config = generate_opencode_config(model, "capability_probe", {"type": "object"})
config["agents"]["capability_probe"].update(steps=3, system="Use read exactly once on fixture.txt, then state the actual failing assertion. No other tools or files.")
config["permissions"].append({"action": "read", "resource": "fixture.txt", "effect": "allow"})
config["policies"].append({"action": "tool.use", "resource": "read:fixture.txt", "effect": "allow"})
config_model = config["providers"]["local"]["models"][model["model_id"]]
config_model["capabilities"]["tools"] = True
config_model["body"].pop("response_format")
config_model["limit"]["output"] = 512
path = root / "opencode.json"
path.write_text(json.dumps(config, indent=2))
env = sanitized_environment(root / "profile")
env["OPENCODE_CONFIG"] = str(path)
exe, version = detect_opencode()
start = time.monotonic()
code, stdout, stderr = _run_opencode([exe, "run", "--standalone", "--model", "local/" + model["model_id"], "--agent", "capability_probe", "--format", "json"], cwd, env, 60,
                                    "Actually call read on fixture.txt, then report the observed failing assertion.")
events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
tools = [e for e in events if e.get("type") == "tool_use"]
completed = [e for e in tools if e.get("part", {}).get("state", {}).get("status") == "completed"]
record = {"exit_code": code, "model": model["model_id"], "latency_seconds": time.monotonic()-start,
          "tool_calls": len(tools), "completed_tools": len(completed), "success": code == 0 and len(completed) == 1,
          "stdout": stdout, "stderr": stderr, "opencode_version": version, "kind": "disposable read-only capability probe"}
(root / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps({k: v for k,v in record.items() if k not in ("stdout", "stderr")}))
