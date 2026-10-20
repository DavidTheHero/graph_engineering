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

    def __init__(self, quote="def add(a,b): return a-b", status="answered_from_repository"):
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
        self.assertIn('calc.py:1 "def add(a,b): return a-b"', note["text"])
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

    def test_trivial_quote_does_not_verify_a_claim(self):
        worker = ClarifyingDouble(quote="def")  # Present on line 1, but proves nothing.
        result, run = self.waiting_ticket(worker)
        _, run, _ = self.ask_small_model(result, run, worker)
        self.assertEqual(run["status"], "needs_information")
        self.assertFalse(run["state"]["question_answers"]["answers"][0]["verified"])

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


class EvidenceHelperTests(unittest.TestCase):
    def test_terms_keep_phrases_and_drop_paths(self):
        from graph_engineering.question_answering import search_terms
        self.assertEqual(search_terms(["model name", "llm.model =", "src/app.js", "  OLLAMA_MODEL ", "ab", "Model Name"]),
                         ["model name", "llm.model =", "OLLAMA_MODEL"])

    def test_citations_verify_only_against_shown_lines(self):
        from graph_engineering.question_answering import verify_reference
        shown = {"calc.py": {1: "def add(a,b): return a-b"}}
        ok = {"file": "calc.py", "line": 1, "quote": "def add(a,b): return a-b"}
        about = "add() subtracts instead of adding"
        self.assertEqual(verify_reference(ok, shown, about), ok)
        self.assertIsNone(verify_reference(ok | {"file": "other.py"}, shown, about))  # A file the model never saw.
        self.assertIsNone(verify_reference(ok | {"quote": "def"}, shown, "def is used"))  # Keyword only.
        self.assertEqual(verify_reference(ok | {"line": 3}, shown, about)["corrected_from"], 3)
        # A short identifier counts only when the answer names it.
        env = {"llm.js": {84: "    model: process.env.OLLAMA_MODEL || 'llama3.1'"}}
        short = {"file": "llm.js", "line": 84, "quote": "model:"}
        self.assertIsNotNone(verify_reference(short, env, "The model: option comes from OLLAMA_MODEL"))
        self.assertIsNone(verify_reference(short, env, "Configured elsewhere"))
        # snake_case and camelCase parts count: OLLAMA_MODEL is about "model".
        whole = {"file": "llm.js", "line": 84, "quote": "process.env.OLLAMA_MODEL"}
        self.assertIsNotNone(verify_reference(whole, env, "Which models are configured?"))

    def test_relevant_citations_in_common_code_shapes_verify(self):
        """Review of PR #3: plurals in -ies, acronym runs, status codes and non-ASCII questions."""
        from graph_engineering.question_answering import verify_reference
        cases = [("const maxRetries = 5;", "Five retry attempts. What is the retry limit?"),
                 ("server = HTTPServer(address, Handler)", "An HTTP server handles it. Which server is used?"),
                 ("return res.status(404).json({})", "A 404 is returned. What does the API return when missing?"),
                 ("model: process.env.OLLAMA_MODEL", "モデルはどこで設定されていますか？")]
        for quote, context in cases:
            with self.subTest(quote=quote):
                shown = {"a.js": {7: "  " + quote}}
                self.assertIsNotNone(verify_reference({"file": "a.js", "line": 7, "quote": quote}, shown, context))
        # Still rejected: keyword-only quotes, and a non-ASCII question cannot rescue a keyword-only quote.
        self.assertIsNone(verify_reference({"file": "a.js", "line": 7, "quote": "import"}, {"a.js": {7: "import"}}, "モデル"))

    def test_real_lines_unrelated_to_the_claim_do_not_verify_it(self):
        """Live Qwen 3.5 9B output on GE-30D05C40 (2026-10-20): genuine import lines cited for an unrelated claim."""
        from graph_engineering.question_answering import verify_reference
        shown = {"backend/server.js": {1: 'import { createApp } from "./app.js";', 2: 'import { createAgent } from "./agent.js";',
                                       5: 'import { createTools } from "./tools.js";'}}
        context = ("The model name is not returned at all by the backend API. Is the model name currently returned by "
                   "the backend API but not displayed in the frontend, or is it not returned at all?")
        for line, quote in ((1, 'import { createApp } from "./app.js";'), (2, 'import { createAgent } from "./agent.js";'),
                            (5, 'import { createTools } from "./tools.js";')):
            self.assertIsNone(verify_reference({"file": "backend/server.js", "line": line, "quote": quote}, shown, context))




class SecretFilterTests(unittest.TestCase):
    setUp = test_intake.IntakeTests.setUp
    tearDown = test_intake.IntakeTests.tearDown

    def test_secret_and_oversized_files_are_never_listed_read_or_grepped(self):
        from graph_engineering.question_answering import focused_evidence, source_files
        from tests.test_core import git
        (self.repo / "config").mkdir()
        (self.repo / "config" / "credentials.py").write_text("API_TOKEN = 'add-super-secret'\n")
        (self.repo / "big.py").write_text("# add\n" + "x = 1\n" * 40000)  # > 180 KB
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Add a secret and a huge file")
        sha = git(self.repo, "rev-parse", "HEAD")
        from graph_engineering.investigation import committed_files
        allowed = committed_files(self.repo, sha)
        listing = source_files(allowed)
        self.assertNotIn("config/credentials.py", listing)
        self.assertNotIn("big.py", listing)
        evidence, shown = focused_evidence(self.repo, sha, ["add"], ["config/credentials.py", "calc.py"], allowed)
        self.assertEqual(evidence["picked_files"], ["calc.py"])
        self.assertNotIn("config/credentials.py", shown)
        self.assertNotIn("big.py", shown)
        self.assertNotIn("config/credentials.py", evidence["files_containing_term"]["add"])
        self.assertNotIn("super-secret", json.dumps(evidence))

    def test_hidden_tail_of_a_long_line_cannot_be_cited(self):
        from graph_engineering.investigation import committed_files
        from graph_engineering.question_answering import SHOWN_WIDTH, focused_evidence, verify_reference
        from tests.test_core import git
        (self.repo / "wide.py").write_text("CONFIG = '" + "x" * SHOWN_WIDTH + "HIDDEN_TAIL_VALUE'\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Long line")
        sha = git(self.repo, "rev-parse", "HEAD")
        evidence, shown = focused_evidence(self.repo, sha, ["CONFIG"], ["wide.py"], committed_files(self.repo, sha))
        self.assertNotIn("HIDDEN_TAIL_VALUE", json.dumps(evidence))  # The model never saw the tail...
        reference = {"file": "wide.py", "line": 1, "quote": "HIDDEN_TAIL_VALUE"}
        self.assertIsNone(verify_reference(reference, shown, "HIDDEN_TAIL_VALUE is set"))  # ...so it cannot cite it.

    def test_old_git_without_max_count_still_searches_and_last_line_has_no_phantom(self):
        from unittest.mock import patch
        from graph_engineering import question_answering as qa
        from graph_engineering.investigation import committed_files
        sha = __import__("tests.test_core", fromlist=["git"]).git(self.repo, "rev-parse", "HEAD")
        real, calls = qa.command, []

        def old_git(argv, *args, **kwargs):
            calls.append(argv)
            if "--max-count=50" in argv:
                return {"returncode": 129, "timed_out": False, "output": "error: unknown option `max-count=50'"}
            return real(argv, *args, **kwargs)
        with patch("graph_engineering.question_answering.command", side_effect=old_git):
            evidence, shown = qa.focused_evidence(self.repo, sha, ["return"], ["calc.py"], committed_files(self.repo, sha))
        self.assertEqual(len(calls), 2)
        self.assertNotIn("search_incomplete", evidence)
        self.assertEqual(evidence["files_containing_term"]["return"][0], "calc.py")
        # calc.py has one line ending in a newline: no phantom empty line 2 is shown or citable.
        self.assertEqual(sorted(shown["calc.py"]), [1])

    def test_line_numbers_match_git_with_form_feeds_and_failed_search_degrades(self):
        from unittest.mock import patch
        from graph_engineering.investigation import committed_files
        from graph_engineering.question_answering import focused_evidence
        from tests.test_core import git
        (self.repo / "ff.py").write_text("a = 1\n\x0c\nb = 2\nTARGET_VALUE = 3\n")  # Form feed on line 2.
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Form feed")
        sha = git(self.repo, "rev-parse", "HEAD")
        allowed = committed_files(self.repo, sha)
        _, shown = focused_evidence(self.repo, sha, ["TARGET_VALUE"], ["ff.py"], allowed)
        self.assertEqual(shown["ff.py"][4], "TARGET_VALUE = 3")  # git grep's line 4, not shifted by splitlines().
        failed = {"returncode": 2, "timed_out": True, "output": ""}
        with patch("graph_engineering.question_answering.command", return_value=failed):
            evidence, shown = focused_evidence(self.repo, sha, ["TARGET_VALUE"], ["ff.py"], allowed)
        self.assertIn("picked files only", evidence["search_incomplete"])
        self.assertIn("ff.py", shown)  # Degraded to the picked file instead of failing the step.


if __name__ == "__main__":
    unittest.main()
