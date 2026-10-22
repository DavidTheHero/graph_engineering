import json
import unittest

from graph_engineering.agent import Agent, validate
from graph_engineering.prompts import FINISH
from graph_engineering.tools import READ_WRITE, Workspace

from helpers import FakeLLM, call, python_repo, temp_dir


class AgentLoop(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.repo = python_repo(self.root)

    def run_agent(self, script, **options):
        llm = FakeLLM(script)
        self.addCleanup(llm.close)
        agent = Agent(llm.model(), Workspace(self.repo), READ_WRITE[:5], FINISH, "system", transcript=self.root / "t.jsonl", **options)
        return agent.run("task"), llm

    def test_tools_run_until_finish(self):
        steps = iter([[call("read_file", path="calc.py")],
                      [call("edit_file", path="calc.py", old_text="return a + b", new_text="return a + b  # sum")],
                      [call("finish", summary="done", coverage=[{"criterion": 1, "tests": "test_add"}])]])
        outcome, llm = self.run_agent(lambda role, messages: next(steps))
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.result["summary"], "done")
        self.assertIn("# sum", (self.repo / "calc.py").read_text())
        tool_results = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertTrue(tool_results[0]["content"].startswith("calc.py (2 lines)"))  # under budget: never rewritten
        self.assertEqual(tool_results[1]["content"], "Edited calc.py")

    def test_invalid_finish_is_returned_to_the_model(self):
        steps = iter([[call("finish", summary="no coverage")],
                      [call("finish", summary="ok", coverage=[])]])
        outcome, llm = self.run_agent(lambda role, messages: next(steps))
        self.assertEqual(outcome.status, "done")
        error = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertIn("missing required field coverage", error)

    def test_repeated_identical_calls_stop_the_run(self):
        outcome, llm = self.run_agent(lambda role, messages: [call("search", pattern="add")])
        self.assertEqual(outcome.status, "looping")
        self.assertEqual(outcome.steps, 4)  # one real call, then three answered from memory
        self.assertIn("You already made this exact call", llm.requests[-1]["messages"][-1]["content"])

    def test_edits_reset_the_repeat_memory(self):
        steps = iter([[call("read_file", path="calc.py")], [call("write_file", path="x.py", content="1")],
                      [call("read_file", path="calc.py")], [call("finish", summary="s", coverage=[])]])
        outcome, llm = self.run_agent(lambda role, messages: next(steps))
        self.assertEqual(outcome.status, "done")
        self.assertTrue(llm.requests[3]["messages"][-1]["content"].startswith("calc.py"))

    def test_cut_off_reasoning_is_carried_forward(self):
        replies = iter([{"message": {"role": "assistant", "content": "", "reasoning": "PLAN: edit calc.py"}, "finish_reason": "length"},
                        [call("finish", summary="s", coverage=[])]])
        outcome, llm = self.run_agent(lambda role, messages: next(replies))
        self.assertEqual(outcome.status, "done")
        sent = llm.requests[1]["messages"]
        self.assertIn("PLAN: edit calc.py", sent[-2]["content"])
        self.assertIn("act on your notes", sent[-1]["content"])

    def test_text_without_tools_stalls_after_four_turns(self):
        outcome, _ = self.run_agent(lambda role, messages: "I am thinking.")
        self.assertEqual(outcome.status, "stalled")
        self.assertEqual(outcome.steps, 4)

    def test_step_budget(self):
        counter = iter(range(100))
        outcome, _ = self.run_agent(lambda role, messages: [call("read_file", path="calc.py", start_line=next(counter) + 1)],
                                    max_steps=3)
        self.assertEqual(outcome.status, "out_of_steps")

    def test_exploration_nudge(self):
        counter = iter(range(100))
        outcome, llm = self.run_agent(lambda role, messages: [call("read_file", path="calc.py", start_line=next(counter) + 1)],
                                      max_steps=4, nudge_after=2)
        self.assertTrue(any("Stop exploring" in m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "user"))

    def test_cancellation(self):
        outcome, _ = self.run_agent(lambda role, messages: [call("read_file", path="calc.py")], cancelled=lambda: True)
        self.assertEqual(outcome.status, "cancelled")

    def test_old_tool_output_is_elided_past_the_budget(self):
        counter = iter(range(100))
        big = "x" * 5000
        (self.repo / "big.txt").write_text("\n".join(big for _ in range(40)))
        llm = FakeLLM(lambda role, messages: [call("read_file", path="big.txt", start_line=next(counter) + 1)])
        self.addCleanup(llm.close)
        agent = Agent(llm.model(), Workspace(self.repo), READ_WRITE[:3], FINISH, "s", max_steps=14)
        agent.history_chars = 200_000
        agent.run("task")
        sent = llm.requests[-1]["messages"]
        self.assertLessEqual(sum(len(json.dumps(m)) for m in sent), 200_000 + 60_000)
        self.assertTrue(sent[2 + 1]["content"].startswith("[elided"))
        self.assertFalse(sent[-1]["content"].startswith("[elided"))

    def test_transcript_records_every_turn(self):
        steps = iter([[call("list_files")], [call("finish", summary="s", coverage=[])]])
        self.run_agent(lambda role, messages: next(steps))
        lines = [json.loads(l) for l in (self.root / "t.jsonl").read_text().splitlines()]
        self.assertEqual([l["role"] for l in lines], ["system", "user", "assistant", "tool", "assistant", "outcome"])


class Validation(unittest.TestCase):
    def test_schema_subset(self):
        schema = FINISH["parameters"]
        self.assertIsNone(validate({"summary": "s", "coverage": [{"criterion": 1, "tests": "t"}]}, schema))
        self.assertIn("coverage", validate({"summary": "s"}, schema))
        self.assertIn("criterion", validate({"summary": "s", "coverage": [{"tests": "t"}]}, schema))
        self.assertIn("type", validate({"summary": 3, "coverage": []}, schema))


if __name__ == "__main__":
    unittest.main()


class Evidence(unittest.TestCase):
    """Finish must cite commands that actually ran after the last change."""

    def setUp(self):
        self.root = temp_dir(self)
        self.repo = python_repo(self.root)

    def run_agent(self, plan, verify=None):
        from graph_engineering.sandbox import Sandbox
        llm = FakeLLM(lambda role, messages: plan[min(sum(m["role"] == "assistant" for m in messages), len(plan) - 1)])
        self.addCleanup(llm.close)
        work = self.root / "box" / "work"
        import shutil
        shutil.copytree(self.repo, work)
        ws = Workspace(work, Sandbox(self.root, work, trusted=True))
        agent = Agent(llm.model(), ws, READ_WRITE, FINISH, "system", evidence=True, max_steps=8, verify=verify)
        return agent.run("task"), llm

    def last_tool_reply(self, llm):
        return [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]

    def test_uncited_or_stale_evidence_is_refused(self):
        test = "python3 -m unittest discover -s tests -q"
        outcome, llm = self.run_agent([
            [call("run", command=test)],
            [call("write_file", path="calc.py", content="def add(a, b):\n    return a + b\n")],
            [call("finish", summary="s", coverage=[], evidence=[{"command": test, "exit_code": 0}])],
            [call("run", command=test)],
            [call("finish", summary="s", coverage=[], evidence=[{"command": test, "exit_code": 0}])]])
        self.assertEqual(outcome.status, "done")
        refusals = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool" and m["content"].startswith("Error")]
        self.assertIn("was not run after your last file change", refusals[0])

    def test_wrong_exit_code_and_missing_evidence_are_refused(self):
        outcome, llm = self.run_agent([
            [call("finish", summary="s", coverage=[])],
            [call("run", command="false")],
            [call("finish", summary="s", coverage=[], evidence=[{"command": "false", "exit_code": 0}])],
            [call("finish", summary="s", coverage=[], evidence=[{"command": "false", "exit_code": 1}])],
            [call("finish", summary="s", coverage=[], evidence=[{"command": "false", "exit_code": 1}], unresolved="it fails")]])
        self.assertEqual(outcome.status, "done")
        errors = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool" and m["content"].startswith("Error")]
        self.assertIn("finish needs evidence", errors[0])
        self.assertIn("last exited 1, not 0", errors[1])
        self.assertIn("no cited command passed", errors[2])


class FinalSteps(unittest.TestCase):
    def test_the_agent_is_told_to_finish_three_steps_before_its_budget(self):
        root = temp_dir(self)
        repo = python_repo(root)
        counter = iter(range(100))

        def script(role, messages):
            if any("3 steps left" in (m.get("content") or "") for m in messages if m["role"] == "user"):
                return [call("finish", summary="partial", coverage=[])]
            return [call("read_file", path="calc.py", start_line=next(counter) + 1)]

        llm = FakeLLM(script)
        self.addCleanup(llm.close)
        outcome = Agent(llm.model(), Workspace(repo), READ_WRITE[:3], FINISH, "s", max_steps=10).run("task")
        self.assertEqual((outcome.status, outcome.steps), ("done", 8))


class LastStep(unittest.TestCase):
    def test_the_last_step_offers_only_the_finish_tool(self):
        root = temp_dir(self)
        repo = python_repo(root)
        counter = iter(range(100))

        def script(role, messages):
            return [call("read_file", path="calc.py", start_line=next(counter) + 1)]  # ignores every warning

        llm = FakeLLM(script)
        self.addCleanup(llm.close)
        Agent(llm.model(), Workspace(repo), READ_WRITE[:3], FINISH, "s", max_steps=5).run("task")
        last = llm.requests[-1]
        self.assertEqual([t["function"]["name"] for t in last["tools"]], ["finish"])
        self.assertEqual(last["tool_choice"], {"type": "function", "function": {"name": "finish"}})


class HarnessVerification(Evidence):
    def test_missing_evidence_is_verified_by_the_harness(self):
        test = "python3 -m unittest discover -s tests -q"
        outcome, llm = self.run_agent([[call("finish", summary="s", coverage=[])]], verify=test)
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.result["evidence"], [{"command": test, "exit_code": 0, "run_by": "harness"}])
        self.assertEqual(len(llm.requests), 1)  # no extra turn

    def test_a_failing_check_is_shown_to_the_agent(self):
        outcome, llm = self.run_agent([[call("finish", summary="s", coverage=[])],
                                       [call("finish", summary="s", coverage=[], unresolved="x", evidence=[{"command": "false", "exit_code": 1}])]],
                                      verify="false")
        reply = [m["content"] for m in llm.requests[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("the harness ran `false` for you", reply)


class ForcedAction(unittest.TestCase):
    def test_after_a_cut_off_the_next_turn_must_act_without_thinking(self):
        root = temp_dir(self)
        repo = python_repo(root)
        replies = iter([{"message": {"role": "assistant", "content": "", "reasoning": "long design"}, "finish_reason": "length"},
                        [call("finish", summary="s", coverage=[])]])
        llm = FakeLLM(lambda role, messages: next(replies))
        self.addCleanup(llm.close)
        model = llm.model()
        model.think = True
        Agent(model, Workspace(repo), READ_WRITE[:3], FINISH, "s").run("task")
        first, second = llm.requests
        self.assertEqual((first["chat_template_kwargs"]["enable_thinking"], first["tool_choice"]), (True, "auto"))
        self.assertEqual((second["chat_template_kwargs"]["enable_thinking"], second["tool_choice"]), (False, "required"))


class ThinkingBudget(unittest.TestCase):
    def test_the_reasoning_cap_is_sent_and_dropped_when_thinking_is_off(self):
        root = temp_dir(self)
        repo = python_repo(root)
        replies = iter([{"message": {"role": "assistant", "content": "", "reasoning": "x"}, "finish_reason": "length"},
                        [call("finish", summary="s", coverage=[])]])
        llm = FakeLLM(lambda role, messages: next(replies))
        self.addCleanup(llm.close)
        model = llm.model()
        model.think = True
        Agent(model, Workspace(repo), READ_WRITE[:3], FINISH, "s", thinking_budget=6144).run("task")
        first, second = llm.requests
        self.assertEqual(first["thinking_token_budget"], 6144)
        self.assertNotIn("thinking_token_budget", second)   # the forced turn runs without thinking
