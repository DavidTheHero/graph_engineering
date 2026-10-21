"""Investigation resolves its own repository questions before anyone is asked (GE-30D05C40, 2026-10-21)."""
import json
from pathlib import Path
import unittest

from graph_engineering.investigation import retrieve
from graph_engineering.repository import git
from graph_engineering.report_store import create
from graph_engineering.workflow import Workflow
from tests import test_intake
from tests.test_intake import InvestigationDouble

REPOSITORY_QUESTION = "Which file implements add()?"


class SelfAnsweringDouble(InvestigationDouble):
    """Labelled inference double: asks a repository question first, answers it once it is handed back."""

    def __init__(self, keeps_asking=False):
        super().__init__()
        self.keeps_asking = keeps_asking
        self.prompts = []

    def invoke(self, role, prompt, schema, model_key, run_id, **kwargs):
        result = super().invoke(role, prompt, schema, model_key, run_id, **kwargs)
        if role != "classification":
            request = json.loads(prompt)
            self.prompts.append(request)
            if self.keeps_asking or not request["open_questions"]:
                result["content"].update(next_action="clarification", questions=[REPOSITORY_QUESTION], read_files=["calc.py"])
        return result


class AutonomousInvestigationTests(unittest.TestCase):
    setUp = test_intake.IntakeTests.setUp
    tearDown = test_intake.IntakeTests.tearDown

    def run_ticket(self, worker):
        result = create(self.tickets, {"repository_id": self.registered["id"], "description": "Adding values gives the wrong total"}, "autonomous-investigation-1")
        flow = Workflow(self.home)
        flow.worker = worker
        try:
            return flow.execute(result["run_id"])
        finally:
            flow.store.close()

    def test_model_answers_its_own_repository_question_and_reaches_review_without_a_person(self):
        worker = SelfAnsweringDouble()
        run = self.run_ticket(worker)
        self.assertEqual(run["status"], "ready_for_review", run["state"].get("error"))
        self.assertEqual(run["state"]["enhanced"]["questions"], [])
        first, second = worker.prompts
        self.assertIn("calc.py", first["repository_files"])  # It can see what exists...
        self.assertNotIn("test_calc.py", first["repository_files"])
        self.assertIn("never ask the user about anything the repository", first["task"])
        self.assertEqual(second["open_questions"], [REPOSITORY_QUESTION])  # ...and gets its question back.
        self.assertEqual(second["evidence"]["excerpts"][0]["file"], "calc.py")  # The file it asked to read comes first.
        events = self.tickets.store.events(run["id"])
        self.assertEqual([e["data"]["questions"] for e in events if e["kind"] == "self_resolution"], [[REPOSITORY_QUESTION]])

    def test_grounded_ticket_with_remaining_questions_goes_to_review_with_them_visible(self):
        worker = SelfAnsweringDouble(keeps_asking=True)
        run = self.run_ticket(worker)
        self.assertEqual(run["status"], "ready_for_review", run["state"].get("error"))
        self.assertEqual(run["state"]["enhanced"]["questions"], [REPOSITORY_QUESTION])
        self.assertEqual(len(worker.prompts), 2)  # Exactly one bounded self-resolution round.

    def test_ungrounded_ticket_still_waits_after_its_self_resolution_round(self):
        worker = InvestigationDouble(clarify=True)  # No findings at all: nothing to review.
        run = self.run_ticket(worker)
        self.assertEqual(run["status"], "needs_information")
        self.assertEqual(sum(m["model_key"] is not None for m in worker.metrics), 3)  # triage + 2 investigations


class RequestedFileRetrievalTests(unittest.TestCase):
    setUp = test_intake.IntakeTests.setUp
    tearDown = test_intake.IntakeTests.tearDown

    def test_requested_committed_files_are_read_first_and_invented_paths_ignored(self):
        body = [f"line_{i} = {i}" for i in range(1, 120)]
        body[10] = "PROMPT = '" + "x" * 6000 + "'"  # GE-30D05C40: one 6 KB line hid line 84 of backend/llm.js.
        (self.repo / "zz_frontend.py").write_text("\n".join(body) + "\n")
        (self.repo / ".env").write_text("SECRET=1\n")
        git(self.repo, "add", "-f", ".")
        git(self.repo, "commit", "-m", "More files")
        sha = git(self.repo, "rev-parse", "HEAD").strip()
        found = retrieve(Path(self.repo), sha, ["unrelated"], "", files=["zz_frontend.py", "invented.py", ".env"])
        files = [e["file"] for e in found["excerpts"]]
        self.assertEqual(files[0], "zz_frontend.py")
        self.assertNotIn("invented.py", files)
        self.assertNotIn(".env", files)
        self.assertEqual(found["excerpts"][0]["start_line"], 1)  # A short requested file is read whole,
        self.assertEqual(found["excerpts"][0]["end_line"], 119)   # past the long line, which is truncated.
        self.assertIn("line_119 = 119", found["excerpts"][0]["text"])
        self.assertIn("…[line truncated]", found["excerpts"][0]["text"].splitlines()[10])


class BudgetAndEvidenceTests(unittest.TestCase):
    """Review of PR #4: the self-resolution round must fit the model budget and keep keyword evidence."""
    setUp = test_intake.IntakeTests.setUp
    tearDown = test_intake.IntakeTests.tearDown

    def test_oversized_investigation_prompt_is_fitted_and_records_what_was_cut(self):
        from graph_engineering.investigation import _size, fit_prompt, prompt_budget
        from graph_engineering.models import default_models
        config = {"models": default_models(), "routing": "baseline"}
        excerpt = lambda i: {"file": f"mod{i}.js", "start_line": 1, "end_line": 160,
                             "numbered_source": "\n".join(f"{n}: const v{n} = \"x\";" for n in range(1, 160))}
        prompt = {"task": "t" * 2500, "repository_files": [f"src/module_{i}.js" for i in range(300)],
                  "open_questions": ["q" * 800] * 8, "original": "o" * 3000, "user_context": {"logs": "l" * 1500},
                  "triage": {}, "evidence": {"excerpts": [excerpt(i) for i in range(8)]},
                  "clarifications": [{"text": "c" * 1000}] * 3, "reproduction_receipt": None, "prior_validation": {}}
        self.assertGreater(_size(prompt), prompt_budget(config))  # Would have failed as "Input exceeds ... budget".
        fitted = fit_prompt(config, prompt)
        self.assertLessEqual(_size(fitted), prompt_budget(config))
        self.assertTrue(fitted["omitted_for_budget"])
        self.assertEqual(fitted["original"], prompt["original"])  # The report itself is never cut.
        self.assertNotIn("omitted_for_budget", fit_prompt(config, prompt | {"repository_files": [], "open_questions": [],
                         "clarifications": [], "evidence": {"excerpts": [excerpt(0)]}}))

    def test_many_long_requested_files_leave_room_for_keyword_evidence(self):
        for i in range(6):
            (self.repo / f"mod{i}.py").write_text("\n".join(f"value_{n} = {n}  # padding text for size" for n in range(400)) + "\n")
        (self.repo / "other.py").write_text("NEEDLE_TERM = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Long files")
        sha = git(self.repo, "rev-parse", "HEAD").strip()
        found = retrieve(Path(self.repo), sha, ["NEEDLE_TERM"], "", files=[f"mod{i}.py" for i in range(6)])
        files = [e["file"] for e in found["excerpts"]]
        self.assertIn("other.py", files)  # The only file containing the search term is not crowded out.
        requested = sum(len(e["text"]) for e in found["excerpts"] if e["file"].startswith("mod"))
        self.assertLessEqual(requested, 12000)
        self.assertTrue(set(found.get("omitted_files", [])) | set(files) >= {f"mod{i}.py" for i in range(6)})

    def test_quote_containing_the_truncation_marker_is_not_source_text(self):
        from graph_engineering.investigation import InvestigationNodes, TRUNCATION_MARKER
        line = "PROMPT = 'xxxx" + TRUNCATION_MARKER
        state = {"enhanced": {"findings": [{"file": "a.py", "line": 1, "quote": "xxxx" + TRUNCATION_MARKER, "observation": "o"}]},
                 "retrieved": {"excerpts": [{"file": "a.py", "start_line": 1, "end_line": 1, "text": line}]},
                 "revision": 1, "intake": {"ticket_id": "GE-1"}}
        updates, _, _ = InvestigationNodes.node_finding_validation(None, state)
        self.assertFalse(updates["finding_validation"]["source_references_valid"])
        genuine = state | {"enhanced": {"findings": [{"file": "a.py", "line": 1, "quote": "PROMPT = 'xxxx", "observation": "o"}]}}
        self.assertTrue(InvestigationNodes.node_finding_validation(None, genuine)[0]["finding_validation"]["source_references_valid"])

    def test_review_card_lists_questions_that_remain_open(self):
        from graph_engineering.attention import item
        from tests.test_attention import DRAFT, row
        found = item(row("ready_for_review", enhanced=DRAFT, finding_validation={"source_references_valid": True, "verified_references": 2}))
        self.assertIn("Open question: Where is the model configured?", found["context"])


if __name__ == "__main__":
    unittest.main()
