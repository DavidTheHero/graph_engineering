"""Actual bounded protocol probes for the discovered Docker 8K model alias."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.models import ModelClient, ModelEndpoint, _request

root = Path(__file__).resolve().parents[1] / "evidence" / "connectivity" / "b8k"
root.mkdir(parents=True, exist_ok=True)
model = ModelEndpoint("http://192.168.0.62:18035/v1", "docker.io/local/qwen35-4b:8k", 8192, output=256, timeout=40, input_budget_bytes=12000)
schema = {"type": "object", "properties": {"language": {"type": "string"}}, "required": ["language"], "additionalProperties": False}
tool = {"type": "function", "function": {"name": "read_evidence", "description": "Read evidence", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}}
client = ModelClient(model)
probes = [
    ("models", lambda: _request(model, "/models")),
    ("invalid_auth_chat", lambda: _request(model, "/chat/completions", {"model": model.model_id, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 32}, invalid_auth=True, timeout=40)),
    ("chat", lambda: client.chat([{"role": "user", "content": "Return JSON only: {\"language\":\"python\"}"}])),
    ("structured", lambda: client.chat([{"role": "user", "content": "Return JSON language python."}], body={"response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}}})),
    ("json_object", lambda: client.chat([{"role": "user", "content": "Return JSON language python."}], body={"response_format": {"type": "json_object"}})),
    ("tool", lambda: client.chat([{"role": "user", "content": "Call read_evidence with path test.log"}], body={"tools": [tool], "tool_choice": "auto"})),
    ("invalid_model", lambda: client.chat([{"role": "user", "content": "Hello"}], body={"model": "graph-engineering-nonexistent-model"})),
    ("invalid_request", lambda: _request(model, "/chat/completions", {"model": model.model_id, "messages": "invalid"})),
    ("output_limit", lambda: client.chat([{"role": "user", "content": "Reply OK"}], body={"max_tokens": 999999}, timeout=10)),
]
for name, probe in probes:
    result = probe()
    (root / (name + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"probe": name, "status": result.get("status"), "latency": result["latency_seconds"], "error": result.get("error"), "body": result.get("body")}), flush=True)
