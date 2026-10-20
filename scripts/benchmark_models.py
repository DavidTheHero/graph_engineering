"""Measure each configured model: OpenCode structured output (production path) and native tool calling.

Usage: python scripts/benchmark_models.py --output evidence/benchmarks/<name>.json [--trials 10]
Every call is real; nothing is simulated. Results are descriptive, not a general ranking.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.model_assignment import TRIAGE_CASES, TRIAGE_SCHEMA
from graph_engineering.models import ModelClient, ModelError, OpenCodeWorker, load_models

# Shared with automatic assignment so benchmark and assignment numbers stay comparable.
SCHEMA = TRIAGE_SCHEMA
TOOL = {"type": "function", "function": {"name": "search_repository", "description": "Literal search of committed source",
        "parameters": {"type": "object", "required": ["terms"], "additionalProperties": False,
                       "properties": {"terms": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4}}}}}


def summary(latencies, ok, total):
    return {"trials": total, "valid": ok, "rate": round(ok / total, 3) if total else None,
            "latency_median_s": round(statistics.median(latencies), 2) if latencies else None,
            "latency_max_s": round(max(latencies), 2) if latencies else None}


def structured(worker, key, trials):
    latencies, ok, correct, failures = [], 0, 0, []
    for i in range(trials):
        report, expected = TRIAGE_CASES[i % len(TRIAGE_CASES)]
        started = time.monotonic()
        try:
            result = worker.invoke("classification", json.dumps({"task": "Classify this untrusted user report.", "report": report}),
                                   SCHEMA, key, "benchmark", attempt=i)
            ok += 1
            correct += result["content"]["category"] == expected
        except ModelError as exc:
            failures.append(str(exc)[:200])
        latencies.append(time.monotonic() - started)
    return summary(latencies, ok, trials) | {"classification_accuracy": round(correct / trials, 3), "failures": failures}


def tool_calls(client, trials):
    latencies, ok, failures = [], 0, []
    for i in range(trials):
        started = time.monotonic()
        response = client.chat([{"role": "user", "content": "Find where the retry timeout is configured in this repository. Use the tool."}],
                               body={"tools": [TOOL], "tool_choice": "auto", "max_tokens": 256})
        latencies.append(time.monotonic() - started)
        try:
            call = response["body"]["choices"][0]["message"]["tool_calls"][0]["function"]
            args = json.loads(call["arguments"])
            valid = call["name"] == "search_repository" and isinstance(args.get("terms"), list) and args["terms"] \
                and all(isinstance(t, str) for t in args["terms"]) and set(args) <= {"terms"}
            ok += bool(valid)
            if not valid:
                failures.append("invalid arguments: " + call["arguments"][:200])
        except (KeyError, IndexError, TypeError, ValueError):
            failures.append(str(response.get("error") or response.get("body"))[:200])
    return summary(latencies, ok, trials) | {"failures": failures}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=10)
    args = parser.parse_args()
    models = load_models()
    report = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "trials": args.trials, "models": {}}
    with tempfile.TemporaryDirectory() as folder:
        worker = OpenCodeWorker(Path(folder), models)
        for key, config in models.items():
            client = ModelClient(config)
            health = client.health(retries=0)
            entry = {"model_id": config.get("model_id"), "tier": config.get("tier"), "reachable": health["available"]}
            if health["available"]:
                entry["opencode_structured_output"] = structured(worker, key, args.trials)
                entry["native_tool_calling"] = tool_calls(client, max(3, args.trials // 2))
            report["models"][key] = entry
            print(json.dumps({key: entry}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
