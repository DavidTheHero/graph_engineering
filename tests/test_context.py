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
            return [call("read_file", path="big.txt", start_line=next(counter) % 16 + 1) for _ in range(3)]

        agent, llm = self.agent(script, notes=self.notes, working_tokens=8000, max_steps=30)
        outcome = agent.run("task " + "t" * 9000)
        self.assertEqual((outcome.status, outcome.result["summary"]), ("done", "after compaction"))
        self.assertEqual(agent.compactions, 1)
        self.assertEqual(self.notes.read_text(), "DONE SO FAR: read big.txt pages. NEXT: finish.")
        compact_request = next(r for r in llm.requests if "Your context is full" in r["messages"][-1]["content"])
        self.assertEqual(compact_request["tool_choice"], "none")
        restarted = llm.requests[-1]["messages"]
        self.assertEqual(len(restarted), 2)
        self.assertIn("DONE SO FAR", restarted[1]["content"])

    def test_tool_result_clearing_comes_before_compaction(self):
        for n in range(15):
            (self.repo / f"mid{n}.txt").write_text("\n".join("z" * 1000 for _ in range(4)))
        steps = 0

        def script(role, messages):
            nonlocal steps
            steps += 1
            if steps > 14:
                return [call("finish", summary="s", coverage=[])]
            return [call("read_file", path=f"mid{steps}.txt")]

        agent, llm = self.agent(script, notes=self.notes, working_tokens=12000, max_steps=20)
        agent.run("task")
        self.assertEqual(agent.compactions, 0)
        self.assertTrue(any(m.get("content", "").startswith("[elided") for m in llm.requests[-1]["messages"] if m["role"] == "tool"))

    def test_a_cleared_result_may_be_requested_again(self):
        """Live failure: clearing said 'repeat the call', the repeat guard refused it, the run ended as looping."""
        (self.repo / "mid.txt").write_text("\n".join("z" * 1000 for _ in range(4)))
        steps = 0

        def script(role, messages):
            nonlocal steps
            steps += 1
            if steps <= 12:
                return [call("read_file", path="mid.txt", start_line=1, end_line=100 + steps)]
            if steps == 13:
                return [call("read_file", path="mid.txt", start_line=1, end_line=101)]  # the first read, long cleared
            return [call("finish", summary="s", coverage=[])]

        agent, llm = self.agent(script, working_tokens=12000, max_steps=20)
        outcome = agent.run("task")
        self.assertEqual(outcome.status, "done")
        replies = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertTrue(replies[-1].startswith("mid.txt"), replies[-1][:80])

    def test_rereading_a_file_keeps_one_current_copy(self):
        """Live failure: with older copies kept, each re-read pushed other files out and the coder never edited."""
        (self.repo / "a.py").write_text("A = 1\n")
        steps = iter([[call("read_file", path="calc.py")], [call("read_file", path="a.py")],
                      [call("read_file", path="calc.py", start_line=1, end_line=50)],
                      [call("edit_file", path="a.py", old_text="A = 1", new_text="A = 2")],
                      [call("read_file", path="calc.py", start_line=1, end_line=2)],
                      [call("finish", summary="s", coverage=[])]])
        agent, llm = self.agent(lambda r, m: next(steps))
        from graph_engineering.tools import READ_WRITE
        agent.tools = READ_WRITE[:5]
        agent.run("task")
        history = json.loads(json.dumps(llm.requests[-1]["messages"]))
        agent.supersede(history)  # what a clearing does once the budget is passed
        tools = [m["content"] for m in history if m["role"] == "tool"]
        self.assertTrue(tools[0].startswith("[an older read of calc.py"), tools[0])
        self.assertTrue(tools[1].startswith("[an older read of a.py, from before your edit"), tools[1])
        self.assertTrue(tools[2].startswith("[an older read of calc.py"), "same lines once clipped to the file's length")
        self.assertTrue(tools[4].startswith("calc.py"))

    def test_history_is_append_only_under_the_budget(self):
        """Rewriting earlier messages breaks the model server's prefix cache; below the budget nothing is rewritten."""
        (self.repo / "a.py").write_text("A = 1\n")
        steps = iter([[call("read_file", path="a.py")], [call("read_file", path="a.py", start_line=1, end_line=5)],
                      [call("read_file", path="calc.py")], [call("finish", summary="s", coverage=[])]])
        agent, llm = self.agent(lambda r, m: next(steps))
        agent.run("task")
        for earlier, later in zip(llm.requests, llm.requests[1:]):
            self.assertEqual(later["messages"][:len(earlier["messages"])], earlier["messages"])

    def test_a_large_result_is_cut_to_a_share_of_a_small_budget(self):
        """Live failure: on the 9B's 16K window two whole-file reads overflowed the budget on their own."""
        (self.repo / "long.py").write_text("\n".join(f"value_{n} = {n}  # " + "x" * 60 for n in range(300)))
        steps = iter([[call("read_file", path="long.py")], [call("finish", summary="s", coverage=[])]])
        agent, llm = self.agent(lambda r, m: next(steps), working_tokens=8000)
        agent.run("task")
        result = next(m for m in llm.requests[-1]["messages"] if m["role"] == "tool")["content"]
        self.assertLessEqual(len(result), agent.result_chars + 200)
        last = int(result.rsplit("start_line=", 1)[1].split()[0]) - 1
        self.assertIn(f"{last}|", result)
        self.assertEqual(list(agent.reads.values())[0][2], last)

    def test_a_long_command_output_keeps_its_end(self):
        from graph_engineering.tools import READ_WRITE

        class Box:
            def run(self, command, timeout=None):
                return 1, "start\n" + "x\n" * 3000 + "FAILED (failures=2)\n", 1.0

        steps = iter([[call("run", command="make test")], [call("finish", summary="s", coverage=[])]])
        llm = FakeLLM(lambda r, m: next(steps))
        self.addCleanup(llm.close)
        agent = Agent(llm.model(), Workspace(self.repo, Box()), READ_WRITE, FINISH, "system", working_tokens=8000)
        agent.run("task")
        result = next(m for m in llm.requests[-1]["messages"] if m["role"] == "tool")["content"]
        self.assertTrue(result.startswith("exit code 1"))
        self.assertIn("FAILED (failures=2)", result)
        self.assertLessEqual(len(result), agent.result_chars)

    def test_compacting_on_consecutive_steps_asks_for_the_final_answer(self):
        """Live failure: the explorer compacted, re-read the same files, and compacted again until it ran out of steps."""
        names = ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt")
        for name in names:
            (self.repo / name).write_text("\n".join("w" * 900 for _ in range(30)))

        def script(role, messages):
            if "Your context is full" in messages[-1]["content"]:
                return "NOTES: the files are long."
            if "This is your last step" in messages[-1]["content"]:
                return [call("finish", summary="from notes", coverage=[])]
            return [call("read_file", path=name) for name in names]

        agent, llm = self.agent(script, notes=self.notes, working_tokens=6000, max_steps=20)
        outcome = agent.run("question " + "q" * 8000)
        self.assertEqual(outcome.status, "done")
        self.assertLessEqual(outcome.steps, 4)
        self.assertEqual(llm.requests[-1]["tool_choice"]["function"]["name"], "finish")

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
        slim = project_brief(self.repo, "M" * 4000, "calc", slim=True)
        self.assertNotIn("Use unittest", slim)  # the explorer reads the guide only if it needs it
        self.assertLess(len(slim), len(project_brief(self.repo, "M" * 4000, "calc")) - 2000)


if __name__ == "__main__":
    unittest.main()


class Routing(unittest.TestCase):
    def test_first_reachable_model_that_fits_and_reviewer_decorrelation(self):
        from graph_engineering.llm import Model, Registry
        small = Model("small", "http://x", "s", context=16384, max_output=4096)
        large = Model("large", "http://y", "l", context=150000, max_output=12288)
        down = Model("down", "http://z", "d", context=150000, max_output=12288)
        registry = Registry({"small": small, "large": large, "down": down},
                            {"explore": ["small", "large"], "review": ["down", "large", "small"]})
        registry.reachable = lambda model: model.name != "down"
        self.assertEqual(registry.for_role("explore", prompt_chars=6000).name, "small")
        self.assertEqual(registry.for_role("explore", prompt_chars=60000).name, "large")   # too big for 16K
        self.assertEqual(registry.for_role("review", prompt_chars=1000).name, "large")     # "down" is unreachable
        self.assertEqual(registry.for_role("review", exclude={"large"}, prompt_chars=1000).name, "small")
        self.assertEqual(registry.for_role("review", exclude={"large"}, prompt_chars=60000).name, "large")  # nothing else fits
