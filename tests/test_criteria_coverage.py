"""Every acceptance criterion is covered by the regression or handed to a follow-up ticket (GE-30D05C40, 2026-10-21).

A four-criterion feature (API endpoint, chat-bar display, selector, switching) was "done" after a regression that
asserted a response field: nothing required the regression to cover the criteria, and every regression went to the
backend folder under node:test, so the UI criteria could never have been tested."""
import json
from pathlib import Path
import tempfile
import unittest

from graph_engineering.accepted_development import coverage_feedback, coverage_summary, regression_runners
from graph_engineering.report_commands import act
from graph_engineering.workflow import Workflow
from tests import test_development_handoff as handoff
from tests.test_development_handoff import DevelopmentDouble


class RunnerDetectionTests(unittest.TestCase):
    def test_vitest_runner_is_offered_beside_node_when_a_script_uses_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "npm run test:backend && npm run test:frontend",
                                                                     "test:backend": "node --test backend/tests/*.test.js",
                                                                     "test:frontend": "vitest run --config frontend/vite.config.js"}}))
            state = {"repository": {"adapter": "javascript", "test_files": ["backend/tests/agent.test.js", "frontend/src/App.test.jsx"]},
                     "baseline_path": folder, "config": {"repo": folder}}
            runners = regression_runners(state, "run1")
            self.assertEqual([r["name"] for r in runners], ["node", "vitest"])
            self.assertEqual((runners[0]["path"], runners[0]["argv"]), ("backend/tests/graph_run1.test.js", ["node", "--test", "backend/tests/graph_run1.test.js"]))
            self.assertEqual(runners[1]["path"], "frontend/src/graph_run1.test.jsx")
            self.assertEqual(runners[1]["argv"], ["npx", "--no-install", "vitest", "run", "--config", "frontend/vite.config.js", "frontend/src/graph_run1.test.jsx"])
            self.assertEqual(runners[1]["example"], "frontend/src/App.test.jsx")
            # Without a vitest script only node is offered; a Python repository gets unittest.
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
            self.assertEqual([r["name"] for r in regression_runners(state, "run1")], ["node"])
            py = regression_runners({"repository": {"adapter": "python", "test_files": ["tests/test_a.py"]}, "baseline_path": folder, "config": {"repo": folder}}, "run1")
            self.assertEqual((py[0]["name"], py[0]["path"]), ("unittest", "tests/test_graph_run1.py"))

    def test_coverage_feedback_requires_every_criterion_exactly_once(self):
        runners = [{"name": "node"}, {"name": "vitest"}]
        criteria = ["endpoint", "chat bar", "selector"]
        ok = {"runner": "node", "covers": [1], "uncovered": [{"criterion": 2, "reason": "needs vitest"}, {"criterion": 3, "reason": "needs vitest"}]}
        self.assertEqual(coverage_feedback(ok, criteria, runners), "")
        self.assertIn("exactly once", coverage_feedback({"runner": "node", "covers": [1], "uncovered": []}, criteria, runners))
        self.assertIn("exactly once", coverage_feedback({"runner": "node", "covers": [1, 2], "uncovered": [{"criterion": 2, "reason": "x"}, {"criterion": 3, "reason": "x"}]}, criteria, runners))
        self.assertIn("at least one", coverage_feedback({"runner": "node", "covers": [], "uncovered": [{"criterion": n, "reason": "x"} for n in (1, 2, 3)]}, criteria, runners))
        self.assertIn("Choose runner", coverage_feedback({"runner": "jest", "covers": [1, 2, 3], "uncovered": []}, criteria, runners))
        self.assertEqual(coverage_feedback({"runner": "node", "covers": [], "uncovered": []}, [], runners), "")  # No criteria recorded: nothing to check.
        summary = coverage_summary({"regression": ok, "enhanced": {"acceptance_criteria": criteria}})
        self.assertEqual((summary["covered"], summary["total"], summary["remaining"]), ([1], 3, [(2, "chat bar", "needs vitest"), (3, "selector", "needs vitest")]))


class PartialDeliveryTests(unittest.TestCase):
    setUp = handoff.DevelopmentHandoffTests.setUp
    tearDown = handoff.DevelopmentHandoffTests.tearDown

    def test_uncovered_criteria_loop_the_author_once_then_continue_in_a_follow_up_ticket(self):
        class PartialAuthor(DevelopmentDouble):
            def __init__(self): super().__init__(); self.proposals = 0; self.prompts = []
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "regression_test":
                    self.proposals += 1; self.prompts.append(json.loads(args[0]))
                    # First proposal forgets criterion 2 entirely; the second hands it to a follow-up.
                    result["content"] |= {"covers": [1], "uncovered": [] if self.proposals == 1 else [{"criterion": 2, "reason": "needs the UI runner"}]}
                return result
        self.tickets.store.db.execute("UPDATE runs SET state=json_set(state,'$.enhanced.acceptance_criteria',json(?)) WHERE id=?",
                                      (json.dumps(["add(9,6) returns 15", "the result is shown in the chat bar"]), self.run["id"]))
        self.tickets.store.db.commit()
        act(self.tickets, self.base | {"action": "start_development", "trust_execution": True}, "partial-start-1")
        flow = Workflow(self.home); worker = PartialAuthor(); flow.worker = worker
        try:
            run = flow.execute(self.run["id"])
        finally:
            flow.store.close()
        self.assertEqual(run["status"], "ready_for_delivery", run["state"].get("error"))
        self.assertEqual(worker.proposals, 2)
        self.assertEqual(worker.prompts[0]["acceptance_criteria_numbered"], ["1. add(9,6) returns 15", "2. the result is shown in the chat bar"])
        self.assertEqual([r["name"] for r in worker.prompts[0]["runners"]], ["unittest"])
        self.assertIn("exactly once", run["state"]["regression_feedback"] if run["state"].get("regression_feedback") else "exactly once")
        self.assertEqual(run["state"]["regression"]["uncovered"], [{"criterion": 2, "reason": "needs the UI runner"}])
        from graph_engineering.attention import items
        [card] = [i for i in items(self.tickets) if i["ticket_id"] == self.ticket["ticket_id"]]
        self.assertTrue(any("remaining: 2. the result is shown in the chat bar" in line for line in card["context"]), card["context"])
        # Deliver and merge locally: completion creates the follow-up ticket once.
        act(self.tickets, self.base | {"action": "publish_development", "approve_publication": True}, "partial-publish-1")
        flow = Workflow(self.home); flow.worker = DevelopmentDouble()
        try:
            run = flow.execute(self.run["id"])
            if run["status"] == "waiting_approval":
                self.tickets.command(self.ticket["ticket_id"], "approve", {"head": run["state"]["head"], "actor": "test-operator"}, "partial-approve-1")
                run = flow.execute(self.run["id"])
        finally:
            flow.store.close()
        self.assertEqual((run["node"], run["status"]), ("done", "completed"), run["state"].get("error"))
        follow = run["state"]["follow_up"]
        created = self.tickets.get(follow["ticket_id"])
        self.assertEqual(follow["remaining"], [2])
        original, options = self.tickets.db.execute("SELECT original, options FROM ticket_reports WHERE ticket_id=?", (follow["ticket_id"],)).fetchone()
        self.assertIn("2. the result is shown in the chat bar", original)
        self.assertIn("Continuation of " + self.ticket["ticket_id"], original)
        self.assertEqual(json.loads(options)["related_ticket"], self.ticket["ticket_id"])
        self.assertEqual(created["run_id"], follow["run_id"])
        # Running completion again (a resumed step) does not create a second ticket.
        flow = Workflow(self.home); flow.run_id = self.run["id"]
        try:
            self.assertEqual(flow.node_completion(run["state"])[0]["follow_up"]["ticket_id"], follow["ticket_id"])
        finally:
            flow.store.close()
        self.assertEqual(sum(1 for e in self.tickets.store.events(self.run["id"]) if e["kind"] == "follow_up_created"), 2)  # Event per completion, ticket once.


if __name__ == "__main__":
    unittest.main()
