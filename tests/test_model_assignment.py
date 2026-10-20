"""Automatic model-to-role assignment: probes, requirements, ranking, safe apply and routing integration."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graph_engineering import cli, model_assignment as ma, routing
from graph_engineering.models import ModelError, default_models, load_models
from tests.test_core import make_repo

STRONG, SLOPPY, BROKEN, CRASH = "strong", "sloppy", "broken", "crash"


def K(model_id):
    """Registry key the controller derives for a newly discovered model id."""
    return ma.model_key(model_id, set())


class ProbeDouble:
    """Labelled test double standing in for OpenCode: behaviour per model key, no network."""

    def __init__(self, behaviour):
        # Behaviour is given per model id; the controller addresses models by registry key.
        self.behaviour = {K(model_id): style for model_id, style in behaviour.items()}
        self.calls = []

    def invoke(self, role, prompt, schema, key, run_id, attempt=0, **_):
        self.calls.append((key, attempt))
        style = self.behaviour[key]
        if style == BROKEN:
            raise ModelError("Worker did not return a strict JSON artifact")
        if style == CRASH:  # The server fails (e.g. GPU out of memory), not the model's answer.
            raise ModelError("OpenCode exited unsuccessfully; inspect retained result", transient=True, phase="exit")
        request = json.loads(prompt)
        if "report" in request:
            category = next(c for r, c in ma.TRIAGE_CASES if r == request["report"])
            return {"content": {"category": category, "rationale": "observed"}}
        fragment = next(f for q, f in ma.GROUNDING_QUESTIONS if q == request["question"])
        line = ma.expected_line(fragment) - (1 if style == SLOPPY else 0)  # Sloppy cites one line off.
        quote = ma.TARGET_SOURCE.splitlines()[line - 1].strip() or "x"
        return {"content": {"file": ma.TARGET_FILE, "line": line, "quote": quote}}


def endpoints(*specs):
    """Patch discovery: {url: [(model_id, context, parameters)]} with None meaning unreachable."""
    table = dict(specs)

    def fake(url, timeout=8):
        models = table[url]
        if models is None:
            return {"endpoint": url, "reachable": False, "error": "URLError: refused", "models": []}
        return {"endpoint": url, "reachable": True, "models": [
            {"model_id": m, "context": c, "parameters_b": p} for m, c, p in models]}
    return patch("graph_engineering.model_assignment.discover", side_effect=fake)


BIG, SMALLURL = "http://big.test/v1", "http://small.test/v1"


class AssessmentFixture(unittest.TestCase):
    """Shared home and helpers; defines no tests so subclasses do not re-run inherited ones."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.folder.cleanup()

    def assess(self, behaviour, *specs, select=None):
        with endpoints(*specs):
            return ma.assess([url for url, _ in specs], home=self.home, select=select, trials=3, worker=ProbeDouble(behaviour))

    def standard(self):
        return self.assess({"qwen3.8-27b": STRONG, "qwen35-4b-8k": STRONG, "qwen3.5-4b-long": STRONG},
                           (BIG, [("qwen3.8-27b", 150000, 27.0)]),
                           (SMALLURL, [("qwen35-4b-8k", 8192, 4.2), ("qwen3.5-4b-long", 262144, 4.2)]))



class AssessmentTests(AssessmentFixture):
    def test_large_model_gets_complex_roles_small_models_get_bounded_roles_first(self):
        plan = self.standard()
        self.assertTrue(plan["complete"])
        self.assertEqual(plan["uncovered_roles"], [])
        for role in ("root_investigation", "planning", "implementation", "regression_test", "deep_review"):
            self.assertEqual(plan["role_routes"][role], [K("qwen3.8-27b")], role)
        # Bounded roles: small models first, the large model as fallback.
        self.assertEqual(plan["role_routes"]["review"][-1], K("qwen3.8-27b"))
        self.assertEqual(set(plan["role_routes"]["review"][:2]), {"qwen35-4b-8k", K("qwen3.5-4b-long")})

    def test_long_context_alone_does_not_make_a_small_model_large(self):
        plan = self.standard()
        long_small = plan["models"][K("qwen3.5-4b-long")]
        self.assertTrue(long_small["long_grounding"]["passed"])
        self.assertEqual(long_small["tier"], routing.SMALL)
        self.assertIn("tier small below required large", plan["decisions"]["implementation"][K("qwen3.5-4b-long")])
        self.assertIn("context 8192 < 32768", plan["decisions"]["planning"]["qwen35-4b-8k"])
        self.assertIsNone(plan["models"]["qwen35-4b-8k"]["long_grounding"])  # Not attempted below 16K.

    def test_ungrounded_and_broken_models_are_excluded_with_reasons(self):
        plan = self.assess({"good-70b": STRONG, "sloppy-34b": SLOPPY, "broken-32b": BROKEN},
                           (BIG, [("good-70b", 131072, 70.0), ("sloppy-34b", 131072, 34.0), ("broken-32b", 131072, 32.0)]))
        self.assertEqual(plan["role_routes"]["implementation"], [K("good-70b")])
        self.assertIn("grounding 0.00", plan["decisions"]["implementation"][K("sloppy-34b")])
        self.assertIn("structured output 0.00", plan["decisions"]["classification"][K("broken-32b")])
        self.assertIn("cited line", plan["models"][K("sloppy-34b")]["grounding"]["failures"][0])
        # Classification needs no grounding, so the sloppy model still serves it.
        self.assertIn(K("sloppy-34b"), plan["role_routes"]["classification"])

    def test_only_small_models_leave_complex_roles_uncovered(self):
        plan = self.assess({"qwen35-4b-8k": STRONG}, (SMALLURL, [("qwen35-4b-8k", 8192, 4.2)]))
        self.assertIn("implementation", plan["uncovered_roles"])
        self.assertEqual(plan["role_routes"]["classification"], ["qwen35-4b-8k"])

    def test_unreachable_endpoint_is_unassessed_and_existing_key_is_kept(self):
        plan = self.assess({"qwen3.8-27b": STRONG}, (BIG, [("qwen3.8-27b", 150000, 27.0)]),
                           ("http://192.168.0.62:18035/v1", None))
        self.assertFalse(plan["complete"])
        self.assertFalse(plan["models"]["b"]["reachable"])  # Same endpoint/model id as the registry's "b".
        self.assertEqual(plan["models"]["b"]["model_id"], default_models()["b"]["model_id"])

    def test_model_selection_limits_probing(self):
        worker = ProbeDouble({"qwen35-4b-8k": STRONG})
        with endpoints((SMALLURL, [("qwen35-4b-8k", 8192, 4.2), ("qwen3.5-4b-long", 262144, 4.2)])):
            plan = ma.assess([SMALLURL], home=self.home, select={SMALLURL: ["qwen35-4b-8k"]}, trials=1, worker=worker)
        self.assertEqual(list(plan["models"]), ["qwen35-4b-8k"])
        self.assertEqual({key for key, _ in worker.calls}, {"qwen35-4b-8k"})


class InfrastructureDuringAssessmentTests(AssessmentFixture):
    """Regression for 2026-10-20: probing a 262K variant exhausted Metal memory and broke its 8K sibling."""

    def test_server_fault_is_unassessed_not_incapable_and_small_context_runs_first(self):
        worker = ProbeDouble({"qwen35-4b-8k": STRONG, "qwen3.5-4b-long": CRASH})
        with endpoints((SMALLURL, [("qwen3.5-4b-long", 262144, 4.2), ("qwen35-4b-8k", 8192, 4.2)])):
            plan = ma.assess([SMALLURL], home=self.home, trials=3, worker=worker)
        self.assertEqual(worker.calls[0][0], "qwen35-4b-8k")  # Smallest context probed first despite listing order.
        self.assertTrue(plan["models"]["qwen35-4b-8k"]["assessed"])
        self.assertIn("qwen35-4b-8k", plan["role_routes"]["classification"])
        crashed = plan["models"][K("qwen3.5-4b-long")]
        self.assertFalse(crashed["assessed"])
        self.assertIn("unassessed", plan["decisions"]["classification"][K("qwen3.5-4b-long")])
        self.assertFalse(plan["complete"])
        with self.assertRaisesRegex(ValueError, "unassessed after infrastructure errors: " + K("qwen3.5-4b-long")):
            ma.apply_plan(plan, self.home, allow_uncovered=True)

    def test_siblings_after_a_server_fault_are_skipped_not_scored(self):
        worker = ProbeDouble({"tiny-1b": CRASH, "qwen35-4b-8k": STRONG})
        with endpoints((SMALLURL, [("tiny-1b", 4096, 1.0), ("qwen35-4b-8k", 8192, 4.2)])):
            plan = ma.assess([SMALLURL], home=self.home, trials=3, worker=worker)
        self.assertEqual({key for key, _ in worker.calls}, {K("tiny-1b")})
        self.assertIn("skipped: endpoint failed while probing", plan["models"]["qwen35-4b-8k"]["skipped"])
        self.assertEqual(plan["roles_by_model"]["qwen35-4b-8k"], [])
        self.assertFalse(plan["complete"])


class ApplyAndRoutingTests(AssessmentFixture):
    def test_apply_refuses_incomplete_or_uncovered_plans(self):
        partial = self.assess({"qwen3.8-27b": STRONG}, (BIG, [("qwen3.8-27b", 150000, 27.0)]), (SMALLURL, None))
        with self.assertRaisesRegex(ValueError, "Assessment incomplete"):
            ma.apply_plan(partial, self.home)
        small_only = self.assess({"qwen35-4b-8k": STRONG}, (SMALLURL, [("qwen35-4b-8k", 8192, 4.2)]))
        with self.assertRaisesRegex(ValueError, "No eligible model for roles"):
            ma.apply_plan(small_only, self.home)
        self.assertFalse((self.home / "routing.json").exists())

    def test_apply_writes_registry_routes_and_updates_registered_repositories(self):
        from graph_engineering.workflow import initialise
        repo = make_repo(Path(self.folder.name))
        before = initialise(repo, self.home)  # Registered before assignment: baseline routing.
        self.assertEqual(json.loads(before.read_text())["routing"], "baseline")
        result = ma.apply_plan(self.standard(), self.home)
        self.assertEqual([Path(p).resolve() for p in result["repositories_updated"]], [before.resolve()])
        config = json.loads(before.read_text())
        self.assertEqual(config["routing"], "assigned")
        registry = load_models(self.home)
        self.assertEqual(registry[K("qwen3.8-27b")]["tier"], "large")
        self.assertIn("implementation", registry[K("qwen3.8-27b")]["roles"])
        self.assertEqual(registry["a"]["roles"], [])  # Previous models stay registered but unassigned.
        # Routing now follows the measured assignment, including health-aware fallback order.
        self.assertEqual(routing.select(config, "implementation")[0], K("qwen3.8-27b"))
        self.assertIn(routing.select(config, "classification")[0], {"qwen35-4b-8k", K("qwen3.5-4b-long")})
        self.assertEqual(routing.select(config, "review", health={"qwen35-4b-8k": False, K("qwen3.5-4b-long"): False})[0],
                         K("qwen3.8-27b"))
        with self.assertRaises(routing.RoutingError):
            routing.select(config, "implementation", health={K("qwen3.8-27b"): False})
        self.assertEqual(list((self.home / "assignments").glob("plan-*.json"))[0].suffix, ".json")

    def test_update_open_runs_moves_only_idle_unfinished_runs_and_records_it(self):
        from graph_engineering.runtime import Store
        from graph_engineering.tickets import Tickets
        from graph_engineering.workflow import initialise
        repo = make_repo(Path(self.folder.name))
        config = json.loads(initialise(repo, self.home).read_text())
        tickets = Tickets(self.home)
        store = tickets.store
        try:
            ids = {}
            for status in ("needs_information", "completed", "running", "leased", "cancelled"):
                ids[status] = run_id = store.create_run(config)["id"]
                tickets.admit(run_id)
                store.db.execute("UPDATE runs SET status=? WHERE id=?", ("ready" if status == "leased" else status, run_id))
            store.db.execute("UPDATE tickets SET lease_owner='worker-1' WHERE run_id=?", (ids["leased"],))
            store.db.commit()
            store.stop(ids["cancelled"])
            legacy = store.create_run(config)["id"]  # A CLI ``serve`` run: no ticket row, no lease to guard it.
        finally:
            store.close()
        result = ma.apply_plan(self.standard(), self.home, update_open_runs=True)
        self.assertEqual([r["run_id"] for r in result["runs_updated"]], [ids["needs_information"]])
        store = Store(self.home)
        try:
            moved = store.get(ids["needs_information"])["state"]["config"]
            self.assertIn(K("qwen3.8-27b"), moved["models"])
            for run_id in [ids[k] for k in ("completed", "running", "leased", "cancelled")] + [legacy]:
                self.assertNotIn(K("qwen3.8-27b"), store.get(run_id)["state"]["config"]["models"], run_id)
            [event] = [e for e in store.events(ids["needs_information"]) if e["kind"] == "model_registry_adopted"]
            self.assertEqual(event["data"]["before"]["b"], config["models"]["b"]["model_id"])
        finally:
            store.close()
        # Without the flag, open runs keep their snapshot.
        self.assertNotIn("runs_updated", ma.apply_plan(self.standard(), self.home))

    def test_new_registrations_inherit_the_applied_assignment(self):
        from graph_engineering.workflow import initialise
        ma.apply_plan(self.standard(), self.home)
        config = json.loads(initialise(make_repo(Path(self.folder.name)), self.home).read_text())
        self.assertEqual((config["routing"], config["role_routes"]["planning"]), ("assigned", [K("qwen3.8-27b")]))

    def test_empty_role_list_means_no_roles(self):
        config = {"routing": "multimodel", "models": {"x": {"model_id": "m", "tier": "large", "roles": []}}}
        with self.assertRaises(routing.RoutingError):
            routing.select(config, "planning")


class HelperTests(unittest.TestCase):
    def test_parameter_inference_and_keys(self):
        self.assertEqual(ma.parameters_b("qwen3.8-27b"), 27.0)
        self.assertEqual(ma.parameters_b("docker.io/local/qwen35-4b:8k"), 4.0)
        self.assertEqual(ma.parameters_b("x", "4.21B"), 4.21)
        self.assertIsNone(ma.parameters_b("mystery-model"))
        self.assertEqual(ma.model_key("huggingface.co/unsloth/qwen3.5-4b-gguf:UD-Q4_K_XL", set()), "qwen3-5-4b-gguf-ud-q4_k_xl")
        self.assertEqual(ma.model_key("a/b", {"b"}), "b-2")

    def test_citation_verifier_is_strict(self):
        fragment = ma.GROUNDING_QUESTIONS[0][1]
        line = ma.expected_line(fragment)
        quote = ma.TARGET_SOURCE.splitlines()[line - 1].strip()
        self.assertTrue(ma.verify_citation({"file": ma.TARGET_FILE, "line": line, "quote": quote}, fragment)[0])
        self.assertFalse(ma.verify_citation({"file": "other.py", "line": line, "quote": quote}, fragment)[0])
        self.assertFalse(ma.verify_citation({"file": ma.TARGET_FILE, "line": line, "quote": "invented text"}, fragment)[0])
        self.assertFalse(ma.verify_citation({"file": ma.TARGET_FILE, "line": 999, "quote": quote}, fragment)[0])

    def test_discovery_reads_vllm_and_docker_model_runner_metadata(self):
        payload = {"available": True, "models": [
            {"id": "qwen3.8-27b", "max_model_len": 150000},
            {"id": "docker.io/local/qwen35-4b:8k", "dmr": {"context_window": 8192, "parameters": "4.21B"}}]}
        with patch("graph_engineering.model_assignment.ModelClient") as client:
            client.return_value.health.return_value = payload
            found = ma.discover("http://x/v1")
        self.assertEqual([(m["model_id"], m["context"], m["parameters_b"]) for m in found["models"]],
                         [("qwen3.8-27b", 150000, 27.0), ("docker.io/local/qwen35-4b:8k", 8192, 4.21)])

    def test_long_probe_prompt_matches_real_investigation_size(self):
        size = len(json.dumps(ma.distractors(22000)))
        self.assertGreater(size, 22000)
        self.assertLess(size, 40000)  # Fits the large-model input budget.


class CLITests(unittest.TestCase):
    def test_dry_run_saves_plan_and_does_not_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            plan = {"complete": True, "uncovered_roles": [], "role_routes": {}, "roles_by_model": {}, "models": {}}
            with patch("graph_engineering.model_assignment.assess", return_value=plan) as assess, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["--home", str(home), "models", "assign", "--endpoint", BIG, "--trials", "2"])
            self.assertEqual(code, 0)
            self.assertEqual(assess.call_args.kwargs["trials"], 2)
            self.assertTrue((home / "model-assignment.json").exists())
            self.assertFalse((home / "routing.json").exists())
            self.assertIn("Dry run", out.getvalue())


if __name__ == "__main__":
    unittest.main()
