"""Controller integration tests use labelled worker doubles, never demo evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graph_engineering.workflow import Workflow, initialise
from graph_engineering.models import ModelError
from tests.test_core import make_repo, git


class WorkerDouble:
    def __init__(self, fail=False, malicious=False):
        self.metrics = []
        self.fail, self.malicious = fail, malicious

    def invoke(self, role, prompt, output_schema, model_key, run_id, **kwargs):
        if self.fail:
            self.metrics.append({"success": False, "model_id": "unit-test-double"})
            raise ModelError("injected unavailable model")
        if role == "investigation":
            content = dict(title="Addition subtracts operands", expected="5", actual="-1", file="calc.py", line=1,
                           severity="low", confidence=1, category="assertion", acceptance=["add(2,3) returns 5"])
        elif role == "planning":
            content = dict(steps=["Correct addition"], files=["calc.py"], acceptance=["Regression passes"])
        elif role == "implementation":
            content = dict(summary="Correct arithmetic", files=[{"path": "../owned.py" if self.malicious else "calc.py", "content": "def add(a,b): return a+b\n"}])
        else:
            content = dict(decision="approve", findings=[], rationale="Actual source diff restores addition")
        metric = {"success": True, "model_key": model_key, "model_id": "unit-test-double", "latency_seconds": 0}
        self.metrics.append(metric)
        return dict(content=content, raw_path="unit-test-double", metrics=metric)


class WorkflowTests(unittest.TestCase):
    def make(self, root):
        repo = make_repo(root)
        config = initialise(repo, root / "controller", trusted_local=True)
        flow = Workflow(root / "controller")
        self.addCleanup(flow.store.close)
        flow.worker = WorkerDouble()
        return repo, config, flow

    @patch("graph_engineering.workflow.ModelClient.health", return_value={"available": True})
    def test_automatic_merge_requires_explicit_trusted_path_scope(self, _):
        for scope, expected in (([], "waiting_approval"), (["calc.py"], "completed")):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as td:
                repo, config, flow = self.make(Path(td))
                settings = json.loads(config.read_text())
                settings.update(auto_merge=True, auto_merge_paths=scope)
                config.write_text(json.dumps(settings))
                run = flow.execute(flow.new_run(config)["id"])
                self.assertEqual(run["status"], expected, run["state"].get("error"))
                self.assertIsNone(flow.store.approved_head(run["id"]))
                flow.store.close()

    @patch("graph_engineering.workflow.ModelClient.health", return_value={"available": True})
    def test_real_git_frozen_regression_approval_merge_and_restart(self, _):
        with tempfile.TemporaryDirectory() as td:
            repo, config, flow = self.make(Path(td))
            run = flow.execute(flow.new_run(config)["id"])
            self.assertEqual(run["status"], "waiting_approval", run["state"].get("error"))
            self.assertFalse(run["state"]["gate"]["allowed"])
            original = run["state"]["base_sha"]
            self.assertEqual(git(repo, "rev-parse", "HEAD"), original)
            backend = flow.backend(run["state"])
            backend.record_check(run["state"]["pr"]["number"], run["state"]["head"], False)
            failed = flow.execute(run["id"])
            self.assertEqual(failed["status"], "blocked")
            self.assertIn("checks failed or pending", failed["state"]["gate"]["reasons"])
            backend.record_check(run["state"]["pr"]["number"], run["state"]["head"], True)
            flow.store.close()
            flow = Workflow(Path(td) / "controller")
            flow.worker = WorkerDouble()
            run = flow.execute(run["id"])
            self.assertEqual(run["status"], "waiting_approval")
            flow.store.approve(run["id"], run["state"]["head"], "unit-test-operator")
            done = flow.execute(run["id"])
            self.assertEqual(done["status"], "completed", done["state"].get("error"))
            self.assertTrue(done["state"]["postmerge"]["passed"])
            objects = json.loads(backend.path.read_text())
            self.assertEqual(len(objects["issues"]), 1)
            self.assertEqual(len(objects["prs"]), 1)
            self.assertEqual(flow.execute(run["id"])["state"]["merge"], done["state"]["merge"])
            flow.store.close()

    @patch("graph_engineering.workflow.ModelClient.health", return_value={"available": True})
    def test_malicious_proposal_is_blocked_and_no_pr_exists(self, _):
        with tempfile.TemporaryDirectory() as td:
            repo, config, flow = self.make(Path(td))
            flow.worker = WorkerDouble(malicious=True)
            run = flow.execute(flow.new_run(config)["id"])
            self.assertEqual(run["status"], "blocked")
            self.assertEqual(run["node"], "isolated_implementation")
            self.assertFalse((Path(td) / "owned.py").exists())
            self.assertNotIn("pr", run["state"])
            flow.store.close()

    def test_unavailable_worker_retry_and_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            repo, config, flow = self.make(Path(td))
            flow.worker = WorkerDouble(fail=True)
            run = flow.execute(flow.new_run(config)["id"])
            self.assertEqual(run["status"], "blocked")
            self.assertEqual(len([e for e in flow.store.events(run["id"]) if e["kind"] == "failed"]), 2)
            flow.store.clear_stop(run["id"])
            flow.worker = WorkerDouble()
            recovered = flow.execute(run["id"], until="task_planning")
            self.assertEqual(recovered["status"], "scanned")
            self.assertIn("issue", recovered["state"])
            flow.store.stop(run["id"])
            self.assertEqual(flow.execute(run["id"])["status"], "cancelled")
            flow.store.close()

    def test_missing_dependency_is_not_published_as_a_source_bug(self):
        with tempfile.TemporaryDirectory() as td:
            repo, config, flow = self.make(Path(td))
            (repo / "calc.py").write_text("import graph_engineering_deliberately_absent_dependency\ndef add(a,b): return a+b\n")
            git(repo, "add", "calc.py")
            git(repo, "commit", "-m", "Require a deliberately missing dependency")
            run = flow.execute(flow.new_run(config)["id"])
            self.assertEqual(run["status"], "blocked")
            self.assertEqual(run["node"], "evidence_collection")
            self.assertNotIn("issue", run["state"])
            self.assertEqual(flow.worker.metrics, [])
            flow.store.close()


if __name__ == "__main__": unittest.main()
