"""Regression tests for the PR #1 code-review findings; each reproduces the reported failure scenario."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from graph_engineering import cli
from graph_engineering.adapters import is_test
from graph_engineering.models import ModelError, default_models, parse_opencode_output, provider_rejection
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import Workflow, initialise, transitions
from tests.test_core import make_repo
from tests.test_workflow import WorkerDouble


class ReassignTests(unittest.TestCase):
    def test_reassignment_is_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            tickets = Tickets(Path(folder))
            try:
                run = tickets.store.create_run({"repo": folder, "backend": "local", "models": default_models(),
                                                "routing": "multimodel"}, "reassign")
                ticket = tickets.admit(run["id"])
                tickets.command(ticket["id"], "reassign", {"role": "review", "model": "a"}, "reassign-key-1")
                self.assertEqual(tickets.store.get("reassign")["state"]["config"]["model_overrides"], {"review": "a"})
                with self.assertRaisesRegex(ValueError, "large-tier"):
                    tickets.command(ticket["id"], "reassign", {"role": "planning", "model": "b"}, "reassign-key-2")
            finally:
                tickets.store.close()


class QuestionAnsweringReturnTests(unittest.TestCase):
    def test_answers_may_return_to_the_recorded_checkpoint_only(self):
        state = {"question_answer_request": {"return_node": "development_setup"}}
        self.assertIn("development_setup", transitions("question_answering", state))
        self.assertNotIn("development_setup", transitions("question_answering"))  # Not without the recorded request.
        self.assertNotIn("merge_everything", transitions("question_answering", {"question_answer_request": {"return_node": "merge_everything"}}))


class ProviderClassificationTests(unittest.TestCase):
    def event(self, status, code):
        body = json.dumps({"error": {"code": code, "message": "x"}})
        return {"type": "error", "error": {"type": "provider", "status": status, "response": {"body": body}}}

    def test_deterministic_rejections_are_permanent_server_faults_transient(self):
        self.assertEqual(provider_rejection(self.event(400, 400)), 400)
        self.assertEqual(provider_rejection(self.event(200, 422)), 422)
        self.assertIsNone(provider_rejection(self.event(200, 500)))  # The 4B "Compute error" seen on 2026-10-20.
        self.assertIsNone(provider_rejection(self.event(429, 429)))
        self.assertIsNone(provider_rejection({"type": "error"}))
        with self.assertRaises(ModelError) as rejected:
            parse_opencode_output(json.dumps(self.event(400, 400)))
        self.assertFalse(rejected.exception.transient)
        with self.assertRaises(ModelError) as crashed:
            parse_opencode_output(json.dumps(self.event(200, 500)))
        self.assertTrue(crashed.exception.transient)


class TestNamingTests(unittest.TestCase):
    def test_words_ending_in_test_are_source(self):
        for name in ("app/Latest.kt", "src/Contest.php", "lib/Attest.java"):
            self.assertFalse(is_test(name), name)
        for name in ("src/AddTest.java", "Tests/FooTests.cs", "spec/BarSpec.scala"):
            self.assertTrue(is_test(name), name)


class ImplementationModelTests(unittest.TestCase):
    @patch("graph_engineering.workflow.ModelClient.health", return_value={"available": True, "attempts": []})
    def test_review_independence_uses_the_model_that_wrote_the_patch(self, _):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = initialise(make_repo(root), root / "controller", trusted_local=True, routing="multimodel")
            config = json.loads(config_path.read_text())
            config["models"]["a2"] = config["models"]["a"] | {"model_id": "second-large"}
            config.update(allow_same_model_review=False, model_overrides={"review": "a2"})
            config_path.write_text(json.dumps(config))
            tickets = Tickets(root / "controller")  # Creates model_observations for health-aware routing.
            flow = Workflow(root / "controller")
            flow.worker = WorkerDouble()
            try:
                run = flow.execute(flow.new_run(config_path)["id"], until="isolated_implementation")
                self.assertEqual(run["state"]["implementation_model"], "a")
                with tickets.db:  # "a" goes down after model_selection: implementation falls back to "a2".
                    tickets.db.execute("INSERT OR REPLACE INTO model_observations VALUES(?,?,?)",
                                       ("a", time.time(), json.dumps({"available": False})))
                flow.store.clear_stop(run["id"])
                run = flow.execute(run["id"])
                self.assertEqual(run["state"]["implementation_model"], "a2")
                self.assertEqual((run["node"], run["status"]), ("independent_review", "blocked"))
                self.assertIn("different-model review", json.dumps(run["state"]["error"]))
            finally:
                flow.store.close()
                tickets.store.close()


class DoctorTests(unittest.TestCase):
    def test_hanging_gh_does_not_crash_doctor(self):
        real = subprocess.run

        def fake(argv, *args, **kwargs):
            if argv[:3] == ["gh", "auth", "status"]:
                raise subprocess.TimeoutExpired(argv, 15)
            return real(argv, *args, **kwargs)
        with patch("graph_engineering.cli.subprocess.run", side_effect=fake), \
             patch("graph_engineering.cli.shutil.which", return_value="/usr/bin/gh"):
            self.assertFalse(cli._gh_authenticated())


if __name__ == "__main__":
    unittest.main()
