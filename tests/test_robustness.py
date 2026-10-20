"""Infrastructure-fault handling: worker watchdog, retry allowance, backoff, dead letter, single controller."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from graph_engineering.dashboard import live_controller
from graph_engineering.models import ModelError, WorkerStalled, _run_opencode
from graph_engineering.report_store import create
from graph_engineering.scheduler import claim, execute_assignment
from graph_engineering.workflow import REGISTRY, Workflow
from tests import test_intake
from tests.test_intake import InvestigationDouble


def python(code):
    return [sys.executable, "-c", code]


class WatchdogTests(unittest.TestCase):
    def test_silent_worker_is_killed_as_a_startup_stall(self):
        started = time.monotonic()
        with self.assertRaises(WorkerStalled) as caught:
            _run_opencode(python("import time; time.sleep(30)"), Path.cwd(), dict(os.environ), timeout=20, startup_timeout=1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((caught.exception.transient, caught.exception.phase), (True, "startup"))

    def test_slow_but_alive_worker_is_not_mistaken_for_a_stall(self):
        code = "import sys, time; print('{\"type\":\"step_start\"}', flush=True); time.sleep(30)"
        with self.assertRaises(ModelError) as caught:
            _run_opencode(python(code), Path.cwd(), dict(os.environ), timeout=2.5, startup_timeout=1)
        self.assertNotIsInstance(caught.exception, WorkerStalled)
        self.assertEqual((caught.exception.transient, caught.exception.phase), (True, "generation"))

    def test_healthy_worker_reads_large_stdin_and_returns_output(self):
        prompt = "x" * 300_000  # Larger than a pipe buffer: stdin is fed concurrently.
        code, out, err = _run_opencode(python("import sys; print(len(sys.stdin.read()))"), Path.cwd(), dict(os.environ),
                                       timeout=20, input_text=prompt, startup_timeout=5)
        self.assertEqual((code, out.strip()), (0, "300000"))


class StallingDouble(InvestigationDouble):
    """Labelled test double: stalls the investigator ``stalls`` times, then answers."""

    def __init__(self, stalls, phase="startup"):
        super().__init__()
        self.stalls, self.phase, self.calls = stalls, phase, 0

    def invoke(self, role, *args, **kwargs):
        if role == "root_investigation":
            self.calls += 1
            if self.calls <= self.stalls:
                raise WorkerStalled("OpenCode produced no output within 45s", transient=True, phase=self.phase)
        return super().invoke(role, *args, **kwargs)


class InfrastructureRetryTests(unittest.TestCase):
    """Real intake graph and git repository fixture (shared with test_intake); only the worker is a double."""
    setUp = test_intake.IntakeTests.setUp
    tearDown = test_intake.IntakeTests.tearDown

    def run_until_settled(self, worker, key="infra-ticket-1", passes=12):
        result = create(self.tickets, {"repository_id": self.registered["id"], "description": "Adding values gives the wrong total"}, key)
        flow = Workflow(self.home)
        flow.worker = worker
        try:
            run = flow.execute(result["run_id"])
            for _ in range(passes):  # Each pass is a later dispatch after the backoff.
                if run["status"] != "ready":
                    break
                run = flow.execute(result["run_id"])
            return result, run, flow.store.events(result["run_id"])
        finally:
            flow.store.close()

    def test_stall_backs_off_without_spending_the_step_budget_then_completes(self):
        worker = StallingDouble(stalls=3)
        result, run, events = self.run_until_settled(worker)
        self.assertEqual(run["status"], "ready_for_review", run["state"].get("error"))
        retries = [e["data"] for e in events if e["kind"] == "retry_backoff"]
        self.assertEqual([r["delay_seconds"] for r in retries], [15.0, 30.0, 60.0])
        self.assertEqual({r["phase"] for r in retries}, {"startup"})
        investigation = [k for k in run["state"]["attempts"] if k.startswith("problem_investigation:")]
        self.assertEqual(run["state"]["attempts"][investigation[0]], 1)  # Only the successful attempt counts.

    def test_exhausted_infrastructure_allowance_dead_letters_with_cause(self):
        _, run, events = self.run_until_settled(StallingDouble(stalls=99))
        self.assertEqual((run["status"], run["node"]), ("blocked", "problem_investigation"))
        self.assertEqual(run["state"]["error"]["category"], "infrastructure_exhausted")
        self.assertEqual(sum(e["kind"] == "retry_backoff" for e in events), 4)
        ticket = next(t for t in self.tickets.list() if t["run_id"] == run["id"])
        queue, reason = self.tickets.queue(ticket)
        self.assertEqual(queue, "dead")
        self.assertIn("Infrastructure retries exhausted (4)", reason)

    def test_manual_retry_resets_the_infrastructure_allowance(self):
        result, run, _ = self.run_until_settled(StallingDouble(stalls=99))
        ticket = next(t for t in self.tickets.list() if t["run_id"] == run["id"])
        self.tickets.command(ticket["id"], "retry", {}, "manual-retry-1")
        self.assertNotIn("infrastructure_failures", self.tickets.store.get(run["id"])["state"])

    def test_non_startup_fault_probes_model_health_for_the_scheduler(self):
        with patch("graph_engineering.workflow.ModelClient") as client:
            client.return_value.health.return_value = {"available": False, "attempts": []}
            _, run, events = self.run_until_settled(StallingDouble(stalls=1, phase="generation"), passes=0)
        self.assertEqual(run["status"], "ready")
        self.assertIn("model_health", [e["kind"] for e in events])
        observed = self.tickets.db.execute("SELECT data FROM model_observations WHERE key='a'").fetchone()
        self.assertFalse(json.loads(observed[0])["available"])

    def test_scheduler_parks_ticket_in_retry_queue_with_explanation(self):
        result = create(self.tickets, {"repository_id": self.registered["id"], "description": "Adding values gives the wrong total"}, "sched-key-1")
        with patch("graph_engineering.workflow.OpenCodeWorker", return_value=StallingDouble(stalls=99)):
            for _ in range(12):  # Real scheduler dispatches, one graph node each, up to the stalled investigator.
                assigned = claim(self.tickets)
                self.assertIsNotNone(assigned)
                self.assertEqual(execute_assignment(self.home, assigned["id"], assigned["lease_owner"]), 0)
                if self.tickets.get(result["ticket_id"])["not_before"] > time.time() + 5:
                    break
        ticket = self.tickets.get(result["ticket_id"])
        self.assertEqual((ticket["node"], ticket["status"]), ("problem_investigation", "ready"))
        self.assertAlmostEqual(ticket["not_before"], ticket["state"]["retry_not_before"], delta=2)
        queue, reason = self.tickets.queue(ticket)
        self.assertEqual(queue, "retry")
        self.assertIn("Automatic infrastructure retry 1/4", reason)
        self.assertIsNone(claim(self.tickets))  # Not dispatched again before the backoff expires.


class ControllerAndDeadlineTests(unittest.TestCase):
    def test_live_controller_requires_fresh_heartbeat_and_existing_process(self):
        from graph_engineering.tickets import Tickets
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.assertIsNone(live_controller(home))
            tickets = Tickets(home)
            try:
                tickets.set_setting("worker_service", {"pid": os.getpid(), "observed": time.time()})
                self.assertEqual(live_controller(home), os.getpid())
                tickets.set_setting("worker_service", {"pid": os.getpid(), "observed": time.time() - 60})
                self.assertIsNone(live_controller(home))
                tickets.set_setting("worker_service", {"pid": 2 ** 22 + 12345, "observed": time.time()})
                self.assertIsNone(live_controller(home))
            finally:
                tickets.store.close()

    def test_model_nodes_outlast_the_model_call_timeout(self):
        from graph_engineering.scheduler import ROLES
        for name in ROLES:
            with self.subTest(node=name):
                self.assertGreaterEqual(REGISTRY[name].timeout_seconds, 300)
        self.assertEqual(REGISTRY["repository_search"].timeout_seconds, 120)


if __name__ == "__main__":
    unittest.main()
