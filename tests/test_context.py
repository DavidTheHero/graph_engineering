"""Context engineering: notes, compaction, the explore sub-agent and the opening brief."""
import json
import unittest

from graph_engineering.agent import Agent
from graph_engineering.prompts import FINISH, project_brief
from graph_engineering.tools import READ_ONLY, Workspace

from helpers import FakeLLM, call, python_repo, temp_dir


class Context(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.repo = python_repo(self.root)
        self.notes = self.root / "notes.md"

    def agent(self, script, **options):
        llm = FakeLLM(script)
        self.addCleanup(llm.close)
        return Agent(llm.model(), Workspace(self.repo), READ_ONLY, FINISH, "system", **options), llm

    def test_notes_are_kept_outside_the_context_and_open_the_next_session(self):
        steps = iter([[call("update_notes", content="PLAN: add subtract in calc.py")], [call("finish", summary="s", coverage=[])]])
        agent, _ = self.agent(lambda r, m: next(steps), notes=self.notes)
        agent.run("task")
        self.assertEqual(self.notes.read_text(), "PLAN: add subtract in calc.py")
        agent, llm = self.agent(lambda r, m: [call("finish", summary="s", coverage=[])], notes=self.notes)
        agent.run("task")
        opening = llm.requests[0]["messages"][1]["content"]
        self.assertIn("<notes>\nPLAN: add subtract in calc.py\n</notes>", opening)
        self.assertIn("update_notes", [t["function"]["name"] for t in llm.requests[0]["tools"]])

    def test_compaction_summarises_into_notes_and_restarts_the_conversation(self):
        (self.repo / "big.txt").write_text("\n".join("y" * 1500 for _ in range(16)))
        counter = iter(range(1000))

        def script(role, messages):
            if "Your context is full" in messages[-1]["content"]:
                return "DONE SO FAR: read big.txt pages. NEXT: finish."
            if "conversation was compacted" in messages[1]["content"]:
                return [call("finish", summary="after compaction", coverage=[])]
            return [call("read_file", path="big.txt", start_line=next(counter) % 16 + 1)]

        agent, llm = self.agent(script, notes=self.notes, working_tokens=8000, max_steps=30)
        outcome = agent.run("task")
        self.assertEqual((outcome.status, outcome.result["summary"]), ("done", "after compaction"))
        self.assertEqual(agent.compactions, 1)
        self.assertEqual(self.notes.read_text(), "DONE SO FAR: read big.txt pages. NEXT: finish.")
        compact_request = next(r for r in llm.requests if "Your context is full" in r["messages"][-1]["content"])
        self.assertEqual(compact_request["tool_choice"], "none")
        restarted = llm.requests[-1]["messages"]
        self.assertEqual(len(restarted), 2)
        self.assertIn("DONE SO FAR", restarted[1]["content"])

    def test_tool_result_clearing_comes_before_compaction(self):
        (self.repo / "mid.txt").write_text("\n".join("z" * 1000 for _ in range(4)))
        counter = iter(range(1000))
        steps = 0

        def script(role, messages):
            nonlocal steps
            steps += 1
            if steps > 14:
                return [call("finish", summary="s", coverage=[])]
            n = next(counter)
            return [call("read_file", path="mid.txt", start_line=1, end_line=100 + n)]  # distinct calls, same output

        agent, llm = self.agent(script, notes=self.notes, working_tokens=12000, max_steps=20)
        agent.run("task")
        self.assertEqual(agent.compactions, 0)
        self.assertTrue(any(m.get("content", "").startswith("[elided") for m in llm.requests[-1]["messages"] if m["role"] == "tool"))

    def test_explore_returns_only_the_answer(self):
        questions = []

        def explorer(question):
            questions.append(question)
            return "Routes are tested with supertest in backend/tests/api.test.js:12."

        steps = iter([[call("explore", question="how are routes tested?")], [call("finish", summary="s", coverage=[])]])
        agent, llm = self.agent(lambda r, m: next(steps), explorer=explorer)
        agent.run("task")
        self.assertEqual(questions, ["how are routes tested?"])
        tool_reply = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertEqual(tool_reply, "Routes are tested with supertest in backend/tests/api.test.js:12.")

    def test_project_brief_has_the_agent_guide_and_the_layout(self):
        (self.repo / "AGENTS.md").write_text("Use unittest. Keep functions pure.")
        (self.repo / "README.md").write_text("readme text")
        brief = project_brief(self.repo)
        self.assertIn("AGENTS.md:\nUse unittest. Keep functions pure.", brief)
        self.assertNotIn("readme text", brief)  # the agent guide wins; the README stays on demand
        self.assertIn("tests/  (2 files)", brief)
        self.assertIn("calc.py", brief)


if __name__ == "__main__":
    unittest.main()
