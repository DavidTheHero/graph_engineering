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


if __name__ == "__main__":
    unittest.main()
