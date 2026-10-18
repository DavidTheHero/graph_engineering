"""Representative fixed task benchmark: direct A versus actual OpenCode A.

These are synthetic controlled benchmark tasks, not repository findings.
B discovery failure is retained separately and never scored as a successful call.
"""
import json
from pathlib import Path
import statistics
import time
from graph_engineering.models import ModelClient, OpenCodeWorker, default_models, validate_artifact

ROOT = Path(__file__).resolve().parent
MODELS = default_models()
TASKS = [
    ("classification", "Classify evidence filenames pyproject.toml, tests/test_math.py. Return language python, test_command python -m unittest discover.",
     {"type": "object", "properties": {"language": {"type": "string"}, "test_command": {"type": "string"}}, "required": ["language", "test_command"], "additionalProperties": False},
     lambda x: x["language"] == "python" and x["test_command"] == "python -m unittest discover"),
    ("implementation", "Controlled synthetic task: Python file arithmetic.py contains:\ndef add(a, b):\n    return a + b + 1\n\nIndependent test assert add(2, 3) == 5 fails with actual 6. Return an object with path arithmetic.py and content containing the complete minimal corrected Python file. Do not alter tests.",
     {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False},
     lambda x: x["path"] == "arithmetic.py" and "return a + b" in x["content"] and "+ 1" not in x["content"]),
    ("review", "Review this actual supplied synthetic diff. Acceptance: add(2,3) equals 5 and tests must remain unchanged. Diff: arithmetic.py return a + b + 1 becomes return a + b. No other changes. Independently observed tests passed. Return approved true and findings array of strings. Evaluate only this diff; do not invent tests or findings.",
     {"type": "object", "properties": {"approved": {"type": "boolean"}, "findings": {"type": "array", "items": {"type": "string"}}}, "required": ["approved", "findings"], "additionalProperties": False},
     lambda x: x["approved"] is True and not x["findings"]),
]

def main():
    worker = OpenCodeWorker(ROOT / "benchmark-sessions", MODELS)
    rows = []
    for repeat in range(2):
        for role, prompt, schema, check in TASKS:
            start = time.monotonic()
            response = ModelClient(MODELS["a"]).chat([{"role": "user", "content": prompt}], body={"response_format": {"type": "json_schema", "json_schema": {"name": "artifact", "strict": True, "schema": schema}}})
            (ROOT / f"baseline-{role}-{repeat}.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
            try:
                content = json.loads(response["body"]["choices"][0]["message"]["content"])
                validate_artifact(content, schema)
                passed = check(content)
            except Exception:
                passed = False
            rows.append({"mode": "direct_a_baseline", "role": role, "repeat": repeat, "success": passed, "latency_seconds": round(time.monotonic()-start, 4)})
            result = worker.invoke(role, prompt, schema, "a", "benchmark", repeat)
            rows.append({"mode": "opencode_a", "role": role, "repeat": repeat, "success": check(result["content"]), "latency_seconds": result["metrics"]["latency_seconds"], "raw_path": result["raw_path"], "model": "qwen3.8-27b"})
            print(json.dumps(rows[-2:]), flush=True)
    summary = {}
    for mode in ("direct_a_baseline", "opencode_a"):
        subset = [r for r in rows if r["mode"] == mode]
        summary[mode] = {"attempts": len(subset), "successes": sum(r["success"] for r in subset), "average_latency_seconds": round(statistics.mean(r["latency_seconds"] for r in subset), 4)}
    report = {"kind": "synthetic task benchmark, actual model responses", "rows": rows, "summary": summary,
              "b": {"available": False, "scored_tasks": 0, "reason": "three /v1/models requests timed out; no invented ID or results"},
              "interpretation": "Tiny controlled sample. OpenCode adds isolation/session overhead. Multi-model quality or speed advantage is not established while B is unavailable."}
    (ROOT / "benchmark-summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
