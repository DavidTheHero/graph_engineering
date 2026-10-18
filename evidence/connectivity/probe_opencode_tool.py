"""Exceptional capability probe: only read of one disposable fixture is allowed.

Production workflow workers continue to deny every tool.
"""
import json
from pathlib import Path
import time
from graph_engineering.models import default_models, detect_opencode, generate_opencode_config, sanitized_environment, _run_opencode

root = Path(__file__).resolve().parent / "opencode-tool-probe"
cwd = root / "cwd"
cwd.mkdir(parents=True, exist_ok=True)
(cwd / "fixture.txt").write_text("FAIL test_add: expected add(2,3)==5; actual 6\n", encoding="utf-8")
config = generate_opencode_config(default_models()["a"], "capability_probe", {"type": "object"})
config["agents"]["capability_probe"].update(steps=3, system="You are a tightly scoped capability probe. Use the read tool exactly once to read fixture.txt, then report its contents. No other tool or file access is allowed.")
config["permissions"].append({"action": "read", "resource": "fixture.txt", "effect": "allow"})
config["policies"].append({"action": "tool.use", "resource": "read:fixture.txt", "effect": "allow"})
model = config["providers"]["local"]["models"]["qwen3.8-27b"]
model["capabilities"]["tools"] = True
model["body"].pop("response_format")
model["limit"]["output"] = 512
config_path = root / "opencode.json"
config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
env = sanitized_environment(root / "profile")
env["OPENCODE_CONFIG"] = str(config_path)
exe, version = detect_opencode()
start = time.monotonic()
code, stdout, stderr = _run_opencode([exe, "run", "--standalone", "--model", "local/qwen3.8-27b", "--agent", "capability_probe", "--format", "json"], cwd, env, 45, "Read fixture.txt using the read tool, then report the failing assertion. You must actually call read, not infer its contents.")
events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
tools = [e for e in events if e.get("type") == "tool_use"]
record = {"exit_code": code, "latency_seconds": round(time.monotonic()-start,4), "stdout": stdout, "stderr": stderr,
          "tool_events": tools, "opencode_version": version, "kind": "disposable read-only capability probe"}
(root / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps(record, indent=2))
