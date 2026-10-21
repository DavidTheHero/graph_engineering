"""The "Needs you" projection: which tickets wait for a person and which actions are offered."""
import json
import tempfile
import unittest
from pathlib import Path

from graph_engineering.attention import item, items
from graph_engineering.capabilities import Capabilities
from graph_engineering.models import default_models
from graph_engineering.report_commands import act
from graph_engineering.report_store import create
from graph_engineering.workflow import Workflow
from tests import test_intake
from tests.test_intake import InvestigationDouble


def row(status, node="ticket_finalisation", mode="intake", **state):
    base = {"config": {"run_mode": mode, "backend": "local", "models": default_models()}, "repairs": 0, "attempts": {},
            "intake": {"generation": 0, "ticket_id": "GE-1"}, "revision": 2}
    return {"id": "GE-1", "run_id": "r1", "priority": "P2", "status": status, "node": node, "state": base | state,
            "lease_owner": None, "cancel": 0, "retries": 0, "max_retries": 3, "repo": "/x", "run_updated": "2026-10-20T10:00:00+00:00"}


DRAFT = {"title": "Show the connected model", "questions": ["Where is the model configured?"],
         "root_cause_hypothesis": "Not exposed by the API", "acceptance_criteria": ["Header shows the model"],
         "problem_statement": "Users cannot see the model.", "limitations": []}


class ProjectionTests(unittest.TestCase):
    def kinds(self, found):
        return [a["id"] for a in found["actions"]]

    def test_running_queued_finished_and_cancelled_tickets_need_nobody(self):
        for status in ("ready", "running", "completed", "cancelled", "no_findings"):
            self.assertIsNone(item(row(status, enhanced=DRAFT)), status)
        leased = row("needs_information", enhanced=DRAFT) | {"lease_owner": "worker"}
        self.assertIsNone(item(leased))

    def test_clarification_offers_answers_small_model_and_respects_budget(self):
        found = item(row("needs_information", enhanced=DRAFT,
                         question_answers={"answers": [{"question": DRAFT["questions"][0], "status": "partially_answered",
                                                        "answer": "backend/llm.js", "verified": True, "references": []}]}))
        self.assertEqual(found["kind"], "clarification")
        self.assertEqual(found["headline"], "Answer 1 question so investigation can continue")
        self.assertEqual(found["questions"][0]["proposal"]["answer"], "backend/llm.js")
        self.assertEqual(self.kinds(found), ["clarify", "answer_questions", "reinvestigate", "reject_enhancement"])
        # Labels come from the configured registry, not a fixed model size.
        self.assertEqual(found["actions"][1]["label"], "Ask " + default_models()["b"]["label"])
        self.assertEqual(found["actions"][0]["payload"], {"ticket_id": "GE-1", "revision": 2, "actor": "Local operator",
                                                          "action": "clarify"})
        exhausted = item(row("needs_information", enhanced=DRAFT, intake={"generation": 3, "ticket_id": "GE-1"}))
        # The server refuses clarifications once the budget is used, so the card must not offer them.
        self.assertEqual(self.kinds(exhausted), ["reject_enhancement"])
        self.assertTrue(exhausted["questions_readonly"])
        self.assertIn("follow-up", exhausted["headline"])

    def test_proposal_names_the_model_that_answered_and_ask_follows_the_route(self):
        models = default_models() | {"c": default_models()["b"] | {"label": "Replacement small model"}}
        config = {"run_mode": "intake", "models": models, "routing": "assigned", "role_routes": {"question_answering": ["c", "a"]}}
        answers = {"model_key": "c", "model_id": models["c"]["model_id"], "answers": [
            {"question": DRAFT["questions"][0], "status": "still_unresolved", "answer": "", "verified": False}]}
        found = item(row("needs_information", enhanced=DRAFT, config=config, question_answers=answers))
        self.assertEqual(found["questions"][0]["proposal"]["model"], "Replacement small model")
        self.assertEqual(found["actions"][1]["label"], "Ask Replacement small model")
        # Key "c" later repointed to another model: the old proposal keeps the id of the model that wrote it.
        older = answers | {"model_id": "docker.io/local/qwen35-4b:8k"}
        found = item(row("needs_information", enhanced=DRAFT, config=config, question_answers=older))
        self.assertEqual(found["questions"][0]["proposal"]["model"], "docker.io/local/qwen35-4b:8k")

    def test_review_accept_is_disabled_without_verified_references(self):
        good = item(row("ready_for_review", enhanced=DRAFT, finding_validation={"source_references_valid": True, "verified_references": 2}))
        self.assertEqual(self.kinds(good)[0], "accept")
        self.assertFalse(good["actions"][0].get("disabled"))
        bad = item(row("ready_for_review", enhanced=DRAFT, finding_validation={"source_references_valid": False}))
        self.assertTrue(bad["actions"][0]["disabled"])

    def test_development_needs_queue_then_explicit_consent_unless_autonomous(self):
        self.assertEqual(self.kinds(item(row("ready_for_development", "development_admission", enhanced=DRAFT))), ["send_development"])
        queued = item(row("development_queued", "development_admission", enhanced=DRAFT))
        self.assertEqual((queued["actions"][0]["input"], queued["actions"][0]["consent_key"]), ("consent", "trust_execution"))
        self.assertIsNone(item(row("development_queued", "development_admission", enhanced=DRAFT), automatic_development=True))

    def test_merge_approval_binds_the_exact_head_and_disappears_once_approved(self):
        waiting = row("waiting_approval", "approval_gate", mode="development", head="a" * 40,
                      verification={"passed": True}, review={"decision": "approve"}, plan={"files": ["calc.py"]})
        found = item(waiting)
        self.assertEqual(found["kind"], "merge_approval")
        self.assertEqual(found["actions"][0]["payload"], {"ticket_id": "GE-1", "action": "approve", "head": "a" * 40})
        self.assertEqual(found["actions"][0]["input"], "actor")
        self.assertIsNone(item(waiting, approved_head="a" * 40))

    def test_rejected_merge_and_final_rejection_leave_the_view(self):
        waiting = row("waiting_approval", "approval_gate", mode="development", head="a" * 40) | {"paused": 1}
        self.assertIsNone(item(waiting))  # "Reject" pauses the ticket: the decision has been made.
        self.assertIsNone(item(row("enhancement_rejected", enhanced=DRAFT, intake={"generation": 3, "ticket_id": "GE-1"})))
        self.assertEqual(self.kinds(item(row("enhancement_rejected", enhanced=DRAFT))), ["reinvestigate"])
        # A paused ticket that is blocked still needs a retry decision.
        paused_blocked = row("blocked", "problem_investigation", error="stalled") | {"paused": 1}
        self.assertEqual(self.kinds(item(paused_blocked)), ["retry"])

    def test_escalated_with_retries_left_is_dead_letter_but_retryable(self):
        found = item(row("escalated", "repair", mode="development", error="Repair budget exhausted"))
        self.assertEqual((found["kind"], self.kinds(found)), ("dead_letter", ["retry"]))

    def test_blocked_and_dead_letter(self):
        blocked = item(row("blocked", "problem_investigation", error={"message": "OpenCode stalled"}))
        self.assertEqual((blocked["kind"], self.kinds(blocked)), ("blocked", ["retry"]))
        self.assertIn("OpenCode stalled", blocked["context"][0])
        dead = item(row("blocked", "problem_investigation") | {"retries": 3})
        self.assertEqual((dead["kind"], dead["actions"]), ("dead_letter", []))

    def test_stopped_development_offers_revision_and_shows_recorded_notes(self):
        notes = [{"text": "drop the role feature", "actor": "op", "at": 1}]
        dead = item(row("blocked", "regression_review", mode="accepted_development", enhanced=DRAFT,
                        error="Regression review rejected twice", development_information=notes) | {"retries": 3})
        self.assertEqual((dead["kind"], self.kinds(dead)),
                         ("dead_letter", ["answer_questions", "development_information", "revise_requirement"]))
        revise = dead["actions"][-1]
        self.assertTrue(revise["primary"] and revise["confirm"])
        self.assertIn("Your note: drop the role feature", dead["context"])
        self.assertFalse(any("scoped follow-up" in line for line in dead["context"]))


class LiveProjectionTests(unittest.TestCase):
    """Real intake graph (labelled inference double): projection, command round-trip and MCP."""
    setUp = test_intake.IntakeTests.setUp
    tearDown = test_intake.IntakeTests.tearDown

    def test_clarification_answer_round_trip_and_mcp_listing(self):
        result = create(self.tickets, {"repository_id": self.registered["id"], "description": "Adding values gives the wrong total"}, "attention-1")
        flow = Workflow(self.home)
        flow.worker = InvestigationDouble(clarify=True)
        try:
            flow.execute(result["run_id"])
        finally:
            flow.store.close()
        [found] = items(self.tickets)
        self.assertEqual((found["ticket_id"], found["kind"]), (result["ticket_id"], "clarification"))
        listed = Capabilities(self.home, "read", "test").call("needs_human")
        self.assertEqual((listed["total"], listed["items"][0]["kind"]), (1, "clarification"))
        send = found["actions"][0]
        act(self.tickets, send["payload"] | {"text": "Q: Provide observed input and output\nA: add(2,3) gives -1"}, "attention-2")
        self.assertEqual(items(self.tickets), [])  # Investigation resumed: nothing waits for a person.
        state = self.tickets.store.get(result["run_id"])["state"]
        self.assertEqual(state["clarifications"][-1]["text"].splitlines()[1], "A: add(2,3) gives -1")


if __name__ == "__main__":
    unittest.main()
