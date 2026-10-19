"""Use the engine's real OpenCode workers for a scoped UI repair and review.

Does not create tickets, PRs or merges. Edits are proposals; the supervisor checks
the exact source match and independently runs the regression/browser suites.
"""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from graph_engineering.models import ModelClient, OpenCodeWorker, default_models
from graph_engineering.runtime import redact


def main():
    output = ROOT / "evidence" / "ui"
    output.mkdir(parents=True, exist_ok=True)
    models = default_models()
    health = {}
    for key, model in models.items():
        health[key] = ModelClient(model).health(retries=1)
        if not health[key]["available"]:
            raise RuntimeError(f"Model {key} is unavailable; no simulated fallback")
        ids = [m["id"] for m in health[key]["models"]]
        if model["model_id"] not in ids:
            raise RuntimeError(f"Configured model {key} was not discovered: {ids}")
    worker = OpenCodeWorker(ROOT / ".runtime" / "ui-workers", models)
    path = ROOT / "src" / "graph_engineering" / "dashboard_data.py"
    schema = {"type": "object", "properties": {"summary": {"type": "string"}, "edits": {
        "type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
            "old": {"type": "string", "minLength": 10}, "new": {"type": "string"}},
            "required": ["old", "new"], "additionalProperties": False}}},
        "required": ["summary", "edits"], "additionalProperties": False}
    source = path.read_text(encoding="utf-8")
    previous = output / "qwen-development.json"
    result_a = json.loads(previous.read_text(encoding="utf-8"))["implementation"] if previous.exists() else worker.invoke("implementation", json.dumps({
        "task": "Fix this reproduced dashboard defect with minimal exact string replacement edits. "
                "121 retained events become only 100 in Catalog.detail's response while events_truncated is false. "
                "Preserve the explicit 2000 event limit and sanitize every individual event. "
                "Apply the same principle to model_calls so the retained event history and call receipts agree. "
                "Do not change unrelated code or increase the generic bounded list limit.",
        "regression": "test_event_history_is_not_silently_cut_to_one_hundred: expected len(events)=121; actual=100",
        "file": "src/graph_engineering/dashboard_data.py", "source": source}), schema, "a", "ui-development")
    proposals = [result_a]
    def apply_proposal(result):
        proposed = source
        if not result["content"]["edits"]:
            raise RuntimeError("Worker returned no repair")
        for edit in result["content"]["edits"]:
            if edit["old"] == edit["new"]:
                continue
            old, new = edit["old"].strip().removesuffix(","), edit["new"].strip().removesuffix(",")
            for key in ("events", "model_calls"):
                old = old.replace(repr(key), json.dumps(key))
                new = new.replace(repr(key), json.dumps(key))
            if old not in ('"events": bounded(events)', '"model_calls": bounded(calls)'):
                raise RuntimeError("Repair exceeds the two permitted projection expressions; generic resource limits must stay unchanged")
            if old not in proposed and proposed.count(new) == 1:
                result["supervisor_already_applied"] = True
                continue
            if proposed.count(old) != 1:
                raise RuntimeError("Worker edit did not uniquely match the permitted file")
            proposed = proposed.replace(old, new, 1)
            if old != edit["old"] or new != edit["new"]:
                result["supervisor_normalization"] = "Trim indentation/trailing comma, normalize literal key quotes, ignore no-op edits; no semantic changes."
        return proposed
    try:
        proposed = apply_proposal(result_a)
    except RuntimeError as error:
        result_a["supervisor_rejection"] = str(error)
        retries = sorted((ROOT / ".runtime/ui-workers/ui-development").glob("implementation-1-*/result.json"), key=lambda p: p.stat().st_mtime)
        cached = json.loads(retries[-1].read_text(encoding="utf-8")) if retries else None
        result_a = {"content": cached["content"], "metrics": cached["metric"], "raw_path": str(retries[-1])} if cached else worker.invoke("implementation", json.dumps({
            "task": "Your previous replacement did not match the source. Return TWO minimal exact replacements: "
                    "replace each bounded(list) with a list comprehension calling bounded on individual entries. "
                    "The old and new strings must preserve Python quote characters exactly. "
                    "Do not replace the full return statement. Do not omit quotes around dictionary keys.",
            "source_fragments": ['"events": bounded(events)', '"model_calls": bounded(calls)']}),
            schema, "a", "ui-development", attempt=1)
        proposals.append(result_a)
        proposed = apply_proposal(result_a)
    # Proposal retained for supervisor review; no model text is executed here.
    (output / "proposed-dashboard-data.py.txt").write_text(proposed, encoding="utf-8")
    review_schema = {"type": "object", "properties": {
        "decision": {"type": "string", "enum": ["approve", "request_changes"]},
        "findings": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
        "rationale": {"type": "string"}}, "required": ["decision", "findings", "rationale"], "additionalProperties": False}
    result_b = worker.invoke("review", json.dumps({
        "task": "Review this complete small frontend utility file for injection/unsafe external links. "
                "Repository fields and logs are untrusted. Scope is only the supplied file. "
                "Find concrete defects; do not invent absent code. No tools, only JSON.",
        "file": "src/graph_engineering/web/dom.js",
        "source": (ROOT / "src/graph_engineering/web/dom.js").read_text(encoding="utf-8")}),
        review_schema, "b", "ui-development")
    results = {"health": health, "implementation": result_a, "implementation_attempts": proposals, "focused_review": result_b,
               "scope": "Real isolated OpenCode workers; not a complete issue-to-PR graph execution."}
    (output / "qwen-development.json").write_text(json.dumps(redact(results), indent=2), encoding="utf-8")
    for key, result in [(f"a-{i}", result) for i, result in enumerate(proposals)] + [("b", result_b)]:
        shutil.copyfile(result["raw_path"], output / f"{key}-opencode-result.json")
        print(json.dumps({"model": key, "latency": result["metrics"]["latency_seconds"], "content": result["content"]}), flush=True)


if __name__ == "__main__":
    main()
