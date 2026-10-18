"""Repeatable real-model local workflows; local approval is explicitly a test policy."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.evaluation import seed_repositories, evaluate_reports
from graph_engineering.workflow import initialise, Workflow


def evaluate_mode(output, mode, merge_local):
    repos = seed_repositories(output / mode / "repositories")
    flow = Workflow(output / mode / "controller")
    reports = []
    for repo in repos:
        started = time.monotonic()
        config_path = initialise(repo, flow.root, trusted_local=True)
        config = json.loads(config_path.read_text())
        config["routing"] = mode
        config_path.write_text(json.dumps(config, indent=2))
        run = flow.new_run(config_path)
        run = flow.execute(run["id"])
        blocks = []
        if run["status"] == "waiting_approval":
            blocks.append({"condition": "missing approval", "status": run["status"], "reasons": run["state"]["gate"]["reasons"]})
            backend = flow.backend(run["state"])
            backend.record_check(run["state"]["pr"]["number"], run["state"]["head"], False)
            failed = flow.execute(run["id"])
            blocks.append({"condition": "injected failed remote check (local backend)", "status": failed["status"], "reasons": failed["state"]["gate"]["reasons"]})
            backend.record_check(run["state"]["pr"]["number"], run["state"]["head"], True)
            flow.store.stop(run["id"])
            cancelled = flow.execute(run["id"])
            flow.store.close()
            flow = Workflow(output / mode / "controller")
            flow.store.clear_stop(run["id"])
            run = flow.execute(run["id"])
            resumed = run["status"] == "waiting_approval" and cancelled["status"] == "cancelled"
            if merge_local and run["status"] == "waiting_approval":
                flow.store.approve(run["id"], run["state"]["head"], "explicit-local-evaluation-policy")
                run = flow.execute(run["id"])
        else:
            resumed = None
        state = run["state"]
        events = flow.store.events(run["id"])
        invocations = [e["data"] for e in events if e["kind"] == "model_call"]
        objects_path = flow.backend(state).path if config["backend"] == "local" else None
        objects = json.loads(objects_path.read_text()) if objects_path and objects_path.exists() else {"issues": [], "prs": []}
        detected = "validation" in state
        report = {"run_id": run["id"], "mode": mode, "repository": str(repo), "stack": state.get("repository", {}).get("adapter"),
                  "status": run["status"], "node": run["node"], "error": state.get("error"),
                  "resolved": run["status"] == "completed", "detection": {"true_positive": int(detected), "false_positive": 0, "false_negative": int(not detected)},
                  "regression": {"before_failed": state.get("baseline", {}).get("returncode", 0) != 0,
                                 "after_passed": bool(state.get("verification", {}).get("passed"))},
                  "duration_seconds": time.monotonic() - started, "retries": len([e for e in events if e["kind"] == "failed"]),
                  "duplicate_issues": max(0, len(objects["issues"])-1), "duplicate_prs": max(0, len(objects["prs"])-1),
                  "merge_policy_violations": int("merge" in state and not state.get("gate", {}).get("allowed")),
                  "gate_demonstrations": blocks, "model_invocations": invocations,
                  "actual_worker_tool_calls": sum(c.get("tool_calls", 0) for c in invocations),
                  "report": state.get("report")}
        if resumed is not None: report["recovery_success"] = resumed
        reports.append(report)
        # The ground truth is controlled by the real seed history; no model labels it.
        if run["status"] == "completed":
            negative = flow.execute(flow.new_run(config_path)["id"])
            reports.append({"run_id": negative["id"], "mode": mode, "repository": str(repo), "stack": state["repository"]["adapter"],
                            "negative_scan": True, "status": negative["status"],
                            "detection": {"false_positive": int(negative["status"] != "no_findings"), "true_negative": int(negative["status"] == "no_findings")}})
        (output / mode / "reports.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(json.dumps({"mode": mode, "repo": repo.name, "run": run["id"], "status": run["status"], "node": run["node"], "error": state.get("error")}), flush=True)
    flow.store.close()
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["baseline", "multimodel", "both"], default="both")
    parser.add_argument("--merge-local", action="store_true", help="explicitly authorise test approvals and real local Git merges; no remote merges")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = {}
    for mode in (["multimodel", "baseline"] if args.mode == "both" else [args.mode]):
        reports[mode] = evaluate_mode(args.output.resolve(), mode, args.merge_local)
    measured = evaluate_reports(reports.get("multimodel", reports.get("baseline", [])), reports.get("baseline") if args.mode == "both" else None)
    all_calls = [c for group in reports.values() for r in group for c in r.get("model_invocations", [])]
    measured["model_invocations"] = {model: {"calls": len(calls := [c for c in all_calls if c["model_id"] == model]),
        "successful_artifacts": sum(c.get("success", False) for c in calls),
        "average_latency_seconds": sum(c.get("latency_seconds", 0) for c in calls)/len(calls)} for model in {c["model_id"] for c in all_calls}}
    measured["scope"] = "Two controlled source-only seeded defects per mode plus fixed-repository negative scans; routing-only baseline. No statistical significance inferred."
    (args.output / "summary.json").write_text(json.dumps(measured, indent=2), encoding="utf-8")
    print(json.dumps(measured, indent=2))


if __name__ == "__main__": main()
