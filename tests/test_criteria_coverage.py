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
            self.assertEqual(runners[1]["argv"][1:], ["node_modules/vitest/vitest.mjs", "run", "--config", "frontend/vite.config.js", "frontend/src/graph_run1.test.jsx"])
            self.assertTrue(runners[1]["argv"][0].endswith("node"))
            self.assertEqual(runners[1]["example"], "frontend/src/App.test.jsx")
            # Review of PR #16: a later command's --config is not vitest's; a script naming a config wins; option-like names are refused.
            for scripts, expected in (({"t": "vitest run && eslint . --config eslint.config.js"}, None),
                                      ({"t": "vitest run; prettier --config .prettierrc ."}, None),
                                      ({"watch": "vitest", "test": "vitest run --config frontend/vite.config.js"}, "frontend/vite.config.js")):
                (root / "package.json").write_text(json.dumps({"scripts": scripts}))
                vitest = regression_runners(state, "run1")[1]["argv"]
                self.assertEqual(vitest[vitest.index("--config") + 1] if "--config" in vitest else None, expected, scripts)
            (root / "package.json").write_text(json.dumps({"scripts": {"t": "vitest run --config -x"}}))
            self.assertEqual([r["name"] for r in regression_runners(state, "run1")], ["node"])
            # Without a vitest script only node is offered; a Python repository gets unittest.
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
            self.assertEqual([r["name"] for r in regression_runners(state, "run1")], ["node"])
            py = regression_runners({"repository": {"adapter": "python", "test_files": ["tests/test_a.py"]}, "baseline_path": folder, "config": {"repo": folder}}, "run1")
            self.assertEqual((py[0]["name"], py[0]["path"]), ("unittest", "tests/test_graph_run1.py"))

    def test_example_test_never_follows_a_symlink(self):
        from graph_engineering.accepted_development import example_test
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder); (root / "src").mkdir()
            (root / "src" / "App.test.jsx").write_text("it('renders', () => {});")
            (Path(outside) / "secret.txt").write_text("host secret")
            (root / "src" / "Leak.test.jsx").symlink_to(Path(outside) / "secret.txt")
            self.assertEqual(example_test(root, "src/App.test.jsx"), "it('renders', () => {});")
            self.assertIsNone(example_test(root, "src/Leak.test.jsx"))
            self.assertIsNone(example_test(root, "../x.test.jsx"))
            self.assertIsNone(example_test(root, None))

    def test_coverage_feedback_requires_every_criterion_exactly_once(self):
        runners = [{"name": "node"}, {"name": "vitest"}]
        criteria = ["1. endpoint", "chat bar", "selector"]
        both = {"tests": [{"runner": "node", "covers": [1]}, {"runner": "vitest", "covers": [2, 3]}], "uncovered": []}
        self.assertEqual(coverage_feedback(both, criteria, runners), "")  # A server test and a UI test cover everything.
        partial = {"tests": [{"runner": "node", "covers": [1]}], "uncovered": [{"criterion": 2, "reason": "x"}, {"criterion": 3, "reason": "x"}]}
        self.assertEqual(coverage_feedback(partial, criteria, runners), "")
        self.assertIn("exactly once", coverage_feedback({"tests": [{"runner": "node", "covers": [1]}], "uncovered": []}, criteria, runners))
        self.assertIn("exactly once", coverage_feedback({"tests": [{"runner": "node", "covers": [1, 2]}], "uncovered": [{"criterion": 2, "reason": "x"}, {"criterion": 3, "reason": "x"}]}, criteria, runners))
        self.assertIn("at least one", coverage_feedback({"tests": [{"runner": "node", "covers": [1, 2, 3]}, {"runner": "vitest", "covers": []}], "uncovered": []}, criteria, runners))
        self.assertIn("exactly once", coverage_feedback({"tests": [{"runner": "node", "covers": [1, 2]}, {"runner": "vitest", "covers": [2, 3]}], "uncovered": []}, criteria, runners))
        self.assertIn("one test file per runner", coverage_feedback({"tests": [{"runner": "node", "covers": [1]}, {"runner": "node", "covers": [2, 3]}], "uncovered": []}, criteria, runners))
        self.assertIn("runner from", coverage_feedback({"tests": [{"runner": "jest", "covers": [1, 2, 3]}], "uncovered": []}, criteria, runners))
        self.assertEqual(coverage_feedback({"tests": [{"runner": "node", "covers": []}], "uncovered": []}, [], runners), "")  # No criteria recorded.
        summary = coverage_summary({"regression": {"parts": [{"path": "a", "covers": [1]}], "uncovered": partial["uncovered"]}, "enhanced": {"acceptance_criteria": criteria}})
        self.assertEqual((summary["covered"], summary["total"], summary["remaining"]), ([1], 3, [(2, "chat bar", "x"), (3, "selector", "x")]))
        from graph_engineering.accepted_development import numbered_criteria
        self.assertEqual(numbered_criteria(criteria), ["1. endpoint", "2. chat bar", "3. selector"])  # Never "1. 1. endpoint".


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
                    result["content"]["tests"][0]["covers"] = [1]
                    result["content"]["uncovered"] = [] if self.proposals == 1 else [{"criterion": 2, "reason": "needs the UI runner"}]
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


class RegressionPerRunnerTests(unittest.TestCase):
    """GE-FDD2275A: one runner per ticket let a UI test stub the server while untested, broken server code shipped.
    A ticket now carries one regression file per runner; every one must fail first and pass at the end."""
    setUp = handoff.DevelopmentHandoffTests.setUp
    tearDown = handoff.DevelopmentHandoffTests.tearDown

    def two_runners(self, state, run_id):
        import sys
        folder = "tests"
        return [{"name": name, "path": f"{folder}/test_graph_{run_id}_{name}.py", "scope": name,
                 "argv": [sys.executable, "-m", "unittest", "discover", "-s", folder, "-p", f"test_graph_{run_id}_{name}.py", "-v"], "example": None}
                for name in ("server", "ui")]

    def author(self, second_passes_on_original=False):
        test = "import unittest\nfrom calc import add\nclass R(unittest.TestCase):\n def test_{0}(self): self.assertEqual(add(9,6),15)\n"
        passing = "import unittest\nclass R(unittest.TestCase):\n def test_ui(self): self.assertTrue(True)\n"
        class TwoFiles(DevelopmentDouble):
            def __init__(self): super().__init__(); self.prompts = []
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role in ("implementation", "review"): self.prompts.append((role, json.loads(args[0])))
                if role == "regression_test":
                    second = passing if second_passes_on_original and not self.prompts else test.format("ui")
                    self.prompts.append((role, json.loads(args[0])))
                    result["content"] = {"tests": [{"runner": "server", "content": test.format("server"), "covers": [1]},
                                                   {"runner": "ui", "content": second, "covers": [2]}], "uncovered": [], "rationale": "r"}
                return result
        return TwoFiles()

    def criteria(self, *items):
        self.tickets.store.db.execute("UPDATE runs SET state=json_set(state,'$.enhanced.acceptance_criteria',json(?)) WHERE id=?",
                                      (json.dumps(list(items)), self.run["id"]))
        self.tickets.store.db.commit()

    def test_both_runner_files_are_frozen_required_and_delivered(self):
        from unittest.mock import patch
        self.criteria("add(9,6) returns 15 on the server", "the UI shows 15")
        act(self.tickets, self.base | {"action": "start_development", "trust_execution": True}, "parts-start-1")
        flow = Workflow(self.home); worker = self.author(); flow.worker = worker
        try:
            with patch("graph_engineering.accepted_development.regression_runners", side_effect=self.two_runners):
                run = flow.execute(self.run["id"])
        finally:
            flow.store.close()
        self.assertEqual(run["status"], "ready_for_delivery", run["state"].get("error"))
        paths = [p["path"] for p in run["state"]["regression"]["parts"]]
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertIn(path, run["state"]["frozen"])
            self.assertIn(path, run["state"]["diff"])
        self.assertEqual([p["path"] for p in run["state"]["verification"]["regression"]["parts"]], paths)  # Both run at the end.
        self.assertEqual(run["state"]["regression"]["covers"], [1, 2])
        planning = [p for role, p in worker.prompts if role == "regression_test"][0]
        self.assertIn("never a stub of it", planning["task"])

    def test_every_runner_file_must_fail_on_the_original_source(self):
        from unittest.mock import patch
        self.criteria("add(9,6) returns 15 on the server", "the UI shows 15")
        act(self.tickets, self.base | {"action": "start_development", "trust_execution": True}, "parts-start-2")
        flow = Workflow(self.home); flow.worker = self.author(second_passes_on_original=True)
        try:
            with patch("graph_engineering.accepted_development.regression_runners", side_effect=self.two_runners):
                run = flow.execute(self.run["id"], until="task_planning")
        finally:
            flow.store.close()
        events = self.tickets.store.events(self.run["id"])
        feedback = [e for e in events if e["node"] == "regression_validation" and e["kind"] == "completed" and e["data"]["next"] == "regression_authoring"]
        self.assertTrue(feedback)
        self.assertIn("_ui.py: It PASSED on the original source", feedback[0]["data"]["updates"]["regression_feedback"])
        self.assertEqual(run["node"], "task_planning")  # Re-authored, then both failed first.

    def test_criteria_left_for_later_are_named_to_the_implementer_and_reviewer(self):
        from graph_engineering.workflow import uncovered_note
        state = {"enhanced": {"acceptance_criteria": ["1. endpoint", "2. chat bar"]}, "regression": {"parts": [{"path": "a", "covers": [2]}], "uncovered": [{"criterion": 1, "reason": "x"}]}}
        note = uncovered_note(state)
        self.assertIn("NOT part of this change", note); self.assertIn("1. endpoint", note); self.assertNotIn("1. 1.", note)
        self.assertEqual(uncovered_note({"enhanced": {"acceptance_criteria": ["a"]}, "regression": {"parts": [{"path": "a", "covers": [1]}], "uncovered": []}}), "")


    def test_an_identical_diff_to_a_rejected_one_is_never_presented_again(self):
        from graph_engineering.runtime import digest
        act(self.tickets, self.base | {"action": "start_development", "trust_execution": True}, "identical-start-1")
        flow = Workflow(self.home); flow.worker = DevelopmentDouble()
        try:
            run = flow.execute(self.run["id"], until="independent_review")
            state = run["state"] | {"delivery_rejections": [{"actor": "op", "reason": "returns nothing", "diff_sha256": digest(run["state"]["diff"])}]}
            flow.run_id = self.run["id"]
            updates, node, _ = flow.node_independent_review(state)
        finally:
            flow.store.close()
        self.assertEqual((node, updates["review"]["approved"]), ("repair", False))
        self.assertIn("Identical to the change rejected at publication by op", updates["review"]["findings"][0]["message"])


class RejectDeliveryTests(unittest.TestCase):
    setUp = handoff.DevelopmentHandoffTests.setUp
    tearDown = handoff.DevelopmentHandoffTests.tearDown

    def test_a_tested_change_can_be_rejected_at_publication_with_a_reason_that_reaches_the_next_development(self):
        from graph_engineering.accepted_development import troubleshooting_context
        from graph_engineering.attention import items
        act(self.tickets, self.base | {"action": "start_development", "trust_execution": True}, "reject-start-1")
        flow = Workflow(self.home); flow.worker = DevelopmentDouble()
        try:
            run = flow.execute(self.run["id"])
        finally:
            flow.store.close()
        self.assertEqual(run["status"], "ready_for_delivery")
        [card] = [i for i in items(self.tickets) if i["ticket_id"] == self.ticket["ticket_id"]]
        self.assertIn("reject_delivery", [a["id"] for a in card["actions"]])
        with self.assertRaisesRegex(ValueError, "Give the reason"):
            act(self.tickets, self.base | {"action": "reject_delivery", "reason": " "}, "reject-empty-1")
        [reject] = [a for a in card["actions"] if a["id"] == "reject_delivery"]
        self.assertEqual(reject["input"], "text")  # The inbox collects free text for it.
        self.tickets.store.db.execute("UPDATE runs SET state=json_set(state,'$.issue',json('{\"number\":7,\"url\":\"u\",\"backend\":\"github\"}')) WHERE id=?", (self.run["id"],))
        self.tickets.store.db.commit()
        act(self.tickets, self.base | {"action": "reject_delivery", "text": "GET /api/model returns nothing"}, "reject-1")
        ticket = self.tickets.get(self.ticket["ticket_id"])
        self.assertEqual((ticket["status"], ticket["state"]["acceptance_cycle"]), ("ready_for_review", 1))
        [rejection] = ticket["state"]["delivery_rejections"]
        self.assertEqual((rejection["reason"], rejection["head"]), ("GET /api/model returns nothing", run["state"]["head"]))
        self.assertTrue(any("rejected at publication" in c for c in troubleshooting_context(ticket["state"])))
        self.assertTrue(any("Publication rejected: GET /api/model" in c["text"] for c in ticket["state"]["clarifications"]))
        self.assertEqual(ticket["state"]["issue"]["number"], 7)  # The issue stays linked: no second issue next cycle.
        self.assertEqual(rejection["diff_sha256"][:8], __import__("graph_engineering.runtime", fromlist=["digest"]).digest(run["state"]["diff"])[:8])
        with self.assertRaisesRegex(ValueError, "awaiting publication"):  # Only a change awaiting publication can be rejected.
            act(self.tickets, self.base | {"action": "reject_delivery", "reason": "again"}, "reject-2")


if __name__ == "__main__":
    unittest.main()
