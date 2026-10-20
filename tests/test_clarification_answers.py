"""Small-model answers for intake tickets waiting for clarification (labelled worker double, real git and graph)."""
import json
import unittest

from graph_engineering.report_commands import act
from graph_engineering.report_store import create
from graph_engineering.workflow import Workflow
from tests import test_intake
from tests.test_intake import InvestigationDouble


class ClarifyingDouble(InvestigationDouble):
    """Asks for clarification until a model-proposal clarification exists; answers questions per ``quote``."""

    def __init__(self, quote="return a-b", status="answered_from_repository"):
        super().__init__(clarify=True)
        self.quote, self.status, self.seen = quote, status, []

    def invoke(self, role, prompt, schema, model_key, run_id, **kwargs):
        self.seen.append((role, model_key, len(prompt.encode()) + len(json.dumps(schema).encode())))
        if role == "question_answering":
            if "search_terms" in schema["properties"]:
                listing = json.loads(prompt)["repository_files"]
                self.listing = listing
                content = {"files": ["calc.py", "invented/path.py"], "search_terms": ["add", "src/calc"]}
            else:
                request = json.loads(prompt)
                self.evidence = request["evidence"]
                content = {"answers": [{"question": q, "answer": "add() returns a-b, so it subtracts.", "status": self.status,
                                        "references": [{"file": "calc.py", "line": 1, "quote": self.quote}]}
                                       for q in request["questions"]]}
            metric = {"success": True, "model_key": model_key, "model_id": "explicit-inference-double", "latency_seconds": 0}
            self.metrics.append(metric)
            return {"content": content, "metrics": metric, "raw_path": "explicit-inference-double"}
        prompt_data = json.loads(prompt)
        self.clarify = not any(c.get("source") == "model_proposal" for c in prompt_data.get("clarifications", []))
        return super().invoke(role, prompt, schema, model_key, run_id, **kwargs)


class ClarificationAnswerTests(unittest.TestCase):
    setUp = test_intake.IntakeTests.setUp
    tearDown = test_intake.IntakeTests.tearDown

    def waiting_ticket(self, worker):
        result = create(self.tickets, {"repository_id": self.registered["id"], "description": "Adding values gives the wrong total"},
                        "clarify-answers-1")
        flow = Workflow(self.home)
        flow.worker = worker
        try:
            run = flow.execute(result["run_id"])
        finally:
            flow.store.close()
        self.assertEqual(run["status"], "needs_information", run["state"].get("error"))
        return result, run

    def ask_small_model(self, result, run, worker):
        accepted = act(self.tickets, {"ticket_id": result["ticket_id"], "action": "answer_questions",
                                      "revision": run["state"]["revision"], "actor": "Local operator"}, "ask-small-1")
        self.assertEqual(self.tickets.store.get(result["run_id"])["state"]["question_answer_request"]["mode"], "clarification")
        flow = Workflow(self.home)
        flow.worker = worker
        try:
            return accepted, flow.execute(result["run_id"]), flow.store.events(result["run_id"])
        finally:
            flow.store.close()

    def test_verified_answers_reinvestigate_and_reach_review(self):
        worker = ClarifyingDouble()
        result, run = self.waiting_ticket(worker)
        _, run, events = self.ask_small_model(result, run, worker)
        self.assertEqual(run["status"], "ready_for_review", run["state"].get("error"))
        answers = run["state"]["question_answers"]
        self.assertTrue(all(a["verified"] for a in answers["answers"]))
        note = run["state"]["clarifications"][-1]
        self.assertEqual(note["source"], "model_proposal")
        self.assertIn('calc.py:1 "return a-b"', note["text"])
        self.assertIn("not operator input", note["text"])
        self.assertEqual(run["state"]["intake"]["generation"], 1)
        # The small model answered within its own budget: no escalation to the large model.
        qa = [s for s in worker.seen if s[0] == "question_answering"]
        self.assertEqual({key for _, key, _ in qa}, {"b"})
        self.assertTrue(all(size <= 6000 for _, _, size in qa), qa)
        self.assertNotIn("degraded_routing", [e["kind"] for e in events])
        self.assertIn("calc.py", [f["file"] for f in worker.evidence["sources"]])
        self.assertEqual(worker.listing, ["calc.py"])  # Real source list, tests excluded.
        self.assertEqual(run["state"]["question_answers"]["picked_files"], ["calc.py"])  # Invented path dropped.
        self.assertEqual(worker.evidence["terms"], ["add"])  # Path-like and generic terms are not grepped.
        self.assertEqual(worker.evidence["files_containing_term"], {"add": ["calc.py", "test_calc.py"]})

    def test_unverifiable_answers_keep_the_ticket_waiting_for_the_user(self):
        worker = ClarifyingDouble(quote="return a*b  # invented")
        result, run = self.waiting_ticket(worker)
        _, run, _ = self.ask_small_model(result, run, worker)
        self.assertEqual((run["node"], run["status"]), ("ticket_finalisation", "needs_information"))
        answer = run["state"]["question_answers"]["answers"][0]
        self.assertEqual((answer["verified"], answer["rejected_references"]), (False, 1))
        self.assertFalse(any(c.get("source") == "model_proposal" for c in run["state"].get("clarifications", [])))

    def test_verified_partial_fact_is_passed_on_and_labelled_partial(self):
        worker = ClarifyingDouble(status="partially_answered")
        result, run = self.waiting_ticket(worker)
        _, run, _ = self.ask_small_model(result, run, worker)
        self.assertEqual(run["status"], "ready_for_review", run["state"].get("error"))
        self.assertIn("A (partial): add() returns a-b", run["state"]["clarifications"][-1]["text"])

    def test_intent_questions_are_not_turned_into_facts(self):
        worker = ClarifyingDouble(status="requires_user_decision")
        result, run = self.waiting_ticket(worker)
        _, run, _ = self.ask_small_model(result, run, worker)
        self.assertEqual(run["status"], "needs_information")
        self.assertFalse(run["state"]["question_answers"]["answers"][0]["verified"])

    def test_further_investigation_budget_is_respected(self):
        worker = ClarifyingDouble()
        result, run = self.waiting_ticket(worker)
        state = run["state"]
        state["intake"]["generation"] = 3
        with self.tickets.db:
            self.tickets.db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), result["run_id"]))
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            act(self.tickets, {"ticket_id": result["ticket_id"], "action": "answer_questions",
                               "revision": state["revision"], "actor": "Local operator"}, "ask-small-2")


if __name__ == "__main__":
    unittest.main()
