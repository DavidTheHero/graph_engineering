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


class SecondReviewTests(unittest.TestCase):
    """Second review pass on e70dde1."""

    def test_liveness_probe_cannot_stop_a_starting_scheduler(self):
        import threading
        from graph_engineering.dashboard import live_controller
        from graph_engineering.runtime import RepoLock
        from graph_engineering.scheduler import acquire_scheduler_lock
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            Tickets(home).store.close()
            probe = RepoLock(home, "scheduler").__enter__()  # A dashboard probe holds it momentarily.
            threading.Timer(0.3, lambda: probe.__exit__(None, None, None)).start()
            lock = acquire_scheduler_lock(home)  # Waits out the probe instead of exiting with BusyError.
            try:
                self.assertEqual(live_controller(home), 0)  # Running, PID not reported yet (not a bogus -1).
            finally:
                lock.__exit__(None, None, None)
            self.assertIsNone(live_controller(home))

    def test_acronym_test_classes_are_tests_and_prefix_words_are_source(self):
        for name in ("src/HTTPTest.java", "IOTest.kt", "DAOTests.cs", "URLSpec.scala", "testcalc.py", "test.js", "test_x.go"):
            self.assertTrue(is_test(name), name)
        for name in ("Testament.kt", "Specification.java", "spectrum.py", "testimonials.php", "app/Latest.kt"):
            self.assertFalse(is_test(name), name)

    def test_assigned_routing_falls_back_only_for_unassessed_roles(self):
        from graph_engineering import routing
        models = default_models()
        older = {"routing": "assigned", "models": models, "role_routes": {"planning": ["a"]}}
        self.assertEqual(routing.select(older, "question_answering")[0], "b")  # Not assessed: tier routing.
        uncovered = older | {"role_routes": {"question_answering": []}}
        with self.assertRaises(routing.RoutingError):  # Assessed with no eligible model: fail closed.
            routing.select(uncovered, "question_answering")

    def test_every_repair_implementer_is_excluded_from_review(self):
        with tempfile.TemporaryDirectory() as folder:
            flow = Workflow(Path(folder))
            try:
                models = default_models()
                models["a2"] = models["a"] | {"model_id": "second-large"}
                config = {"repo": folder, "backend": "local", "models": models, "routing": "multimodel",
                          "allow_same_model_review": False, "model_overrides": {"review": "a"}}
                flow.run_id = flow.store.create_run(config, "implementers")["id"]
                state = {"config": config, "implementation_model": "a2", "implementation_models": ["a", "a2"]}
                with self.assertRaisesRegex(ValueError, "different-model review"):
                    flow.node_independent_review(state)
            finally:
                flow.store.close()

    def test_provider_error_shapes_and_precedence(self):
        api = {"type": "error", "error": {"name": "APIError", "data": {"statusCode": 401, "isRetryable": False}}}
        self.assertEqual(provider_rejection(api), 401)
        api["error"]["data"]["isRetryable"] = True
        self.assertIsNone(provider_rejection(api))
        wrapped = {"type": "error", "error": {"status": 400, "response": {"body": json.dumps({"error": {"code": 429}})}}}
        self.assertIsNone(provider_rejection(wrapped))  # A rate limit inside a 400 wrapper stays retryable.

    def test_permanent_rejection_falls_back_to_the_next_permitted_model(self):
        class RejectA:
            metrics = []

            def invoke(self, role, prompt, schema, key, run_id, **_):
                if key == "a":
                    raise ModelError("OpenCode emitted an error event (provider rejected request: HTTP 404)",
                                     transient=False, phase="provider")
                return {"content": {"ok": True}, "metrics": {"model_key": key}, "raw_path": "double"}
        with tempfile.TemporaryDirectory() as folder:
            flow = Workflow(Path(folder))
            try:
                models = default_models()
                models["a2"] = models["a"] | {"model_id": "second-large"}
                config = {"repo": folder, "backend": "local", "models": models, "routing": "multimodel"}
                flow.run_id = flow.store.create_run(config, "rejection")["id"]
                flow.worker = RejectA()
                schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
                with patch("graph_engineering.workflow.RepoLock"):
                    self.assertEqual(flow.ask({"config": config, "repairs": 0}, "planning", {"task": "x"}, schema, "large"), {"ok": True})
                self.assertEqual(flow.last_model, "a2")
                self.assertIn("provider_rejection_fallback", [e["kind"] for e in flow.store.events("rejection")])
            finally:
                flow.store.close()

    def test_health_sweep_marks_a_removed_model_unavailable(self):
        from graph_engineering.scheduler import observe_models
        with tempfile.TemporaryDirectory() as folder:
            tickets = Tickets(Path(folder))
            try:
                with patch("graph_engineering.scheduler.ModelClient") as client:
                    client.return_value.health.return_value = {"available": True, "models": [{"id": "something-else"}]}
                    observed = observe_models(tickets)
                self.assertFalse(observed["a"]["available"])
                self.assertIn("not advertised", observed["a"]["reason"])
            finally:
                tickets.store.close()

    def test_published_graph_documents_the_dynamic_return_edge(self):
        from graph_engineering.capabilities import Capabilities
        with tempfile.TemporaryDirectory() as folder:
            graph = Capabilities(Path(folder), "read", "t").call("workflow_graph")
        qa = next(n for n in graph["nodes"] if n["name"] == "question_answering")
        self.assertTrue(qa["dynamic_transitions"][0].startswith("<recorded checkpoint>"))
        self.assertFalse(any(t.startswith("<") for t in qa["transitions"]))
