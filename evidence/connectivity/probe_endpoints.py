"""Bounded, replayable raw connectivity probes; credentials are never persisted."""
import concurrent.futures
import datetime
import json
import os
import sys
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
SERVERS = {"a": "http://192.168.0.60:18020/v1", "b": "http://192.168.0.62:18035/v1"}

def request(name, url, payload=None, auth=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + auth
    start = time.monotonic()
    record = {"probe": name, "url": url, "request": payload,
              "auth": "absent" if not auth else "provided", "utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            record.update(status=response.status, body=json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        record.update(status=exc.code, body=body)
    except Exception as exc:
        record.update(error=type(exc).__name__ + ": " + str(exc))
    record["latency_seconds"] = round(time.monotonic() - start, 4)
    (ROOT / (name + ".json")).write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("probe", "status", "error", "latency_seconds") if k in record}), flush=True)
    return record

def probe(label, base):
    models = request(label + "_models_no_auth", base + "/models", timeout=12)
    if models.get("status") != 200:
        for retry in range(2):
            models = request(label + "_models_retry_" + str(retry + 1), base + "/models", timeout=8)
            if models.get("status") == 200:
                break
    if models.get("status") != 200:
        return
    model = models["body"]["data"][0]["id"]
    request(label + "_models_invalid_auth", base + "/models", auth="graph-engineering-intentionally-invalid", timeout=12)
    common = {"model": model, "max_tokens": 256, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    request(label + "_chat", base + "/chat/completions", {**common, "messages": [{"role": "user", "content": "Compute 17*23. Reply with only the integer."}]})
    schema = {"type": "object", "properties": {"language": {"type": "string", "enum": ["python"]}, "test_command": {"type": "string"}}, "required": ["language", "test_command"], "additionalProperties": False}
    request(label + "_structured", base + "/chat/completions", {**common, "messages": [{"role": "user", "content": "Return a JSON object: language python, test_command python -m unittest discover."}], "response_format": {"type": "json_schema", "json_schema": {"name": "repo", "strict": True, "schema": schema}}})
    tool = {"type": "function", "function": {"name": "read_evidence", "description": "Read the selected evidence file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}}
    response = request(label + "_tool", base + "/chat/completions", {**common, "messages": [{"role": "user", "content": "Call read_evidence with path test.log. Do not answer in prose."}], "tools": [tool], "tool_choice": "auto"})
    try:
        message = response["body"]["choices"][0]["message"]
        call = message["tool_calls"][0]
        request(label + "_tool_roundtrip", base + "/chat/completions", {**common, "messages": [{"role": "user", "content": "Read test.log with the tool then identify failing assertion."}, message, {"role": "tool", "tool_call_id": call["id"], "content": "FAIL test_add: expected add(2,3)==5; actual 6"}], "tools": [tool], "tool_choice": "none"})
    except (KeyError, IndexError, TypeError):
        pass
    request(label + "_invalid_model", base + "/chat/completions", {**common, "model": "graph-engineering-nonexistent-model", "messages": [{"role": "user", "content": "Hello"}]})
    request(label + "_invalid_request", base + "/chat/completions", {"model": model, "messages": "must-be-array"})
    request(label + "_output_limit", base + "/chat/completions", {**common, "max_tokens": 999999, "messages": [{"role": "user", "content": "Hello"}]}, timeout=12)

if __name__ == "__main__":
    selected = {key: value for key, value in SERVERS.items() if len(sys.argv) == 1 or key in sys.argv[1:]}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda item: probe(*item), selected.items()))
