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
        # One real call and three answered from memory, a fresh start from the notes, then the same again.
        self.assertEqual(outcome.steps, 8)
        self.assertIn("search is not offered on your next step", llm.requests[-1]["messages"][-1]["content"])

    def test_a_repeated_tool_is_not_offered_on_the_next_step(self):
        """Run 17: told its call was a repeat, a coder made it three more times while saying it would change course."""
        steps = iter([[call("search", pattern="add")], [call("search", pattern="add")], [call("search", pattern="add")],
                      [call("read_file", path="calc.py")], [call("finish", summary="s", coverage=[])]])
        outcome, llm = self.run_agent(lambda role, messages: next(steps))
        self.assertEqual(outcome.status, "done")
        offered = [[t["function"]["name"] for t in r["tools"]] for r in llm.requests]
        self.assertIn("search", offered[1])
        self.assertNotIn("search", offered[2])   # withheld after the repeat at step 2
        self.assertNotIn("search", offered[3])   # and still, after the model repeated it anyway at step 3
        self.assertIn("search", offered[4])      # back once the model took a different step
        replies = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertIn("search is not offered on your next step", replies[1])
        self.assertTrue(replies[2].startswith("You already made this exact call"))

    def test_an_edit_that_restores_an_earlier_content_counts_as_a_repeat(self):
        """Run 17: a coder applied an edit and its exact inverse twenty times over; each one changed the file."""
        forward = call("edit_file", path="calc.py", old_text="return a + b", new_text="return a + b  # sum")
        back = call("edit_file", path="calc.py", old_text="return a + b  # sum", new_text="return a + b")
        steps = iter([[forward], [back], [call("read_file", path="calc.py")], [forward], [call("finish", summary="s", coverage=[])]])
        outcome, llm = self.run_agent(lambda role, messages: next(steps))
        self.assertEqual(outcome.status, "done")
        replies = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertEqual(replies[0], "Edited calc.py")
        self.assertIn("calc.py has the content it had before step 1 again", replies[1])
        self.assertIn("calc.py has the content it had after step 1 again", replies[3])
        self.assertNotIn("edit_file", [t["function"]["name"] for t in llm.requests[2]["tools"]])

    def test_a_loop_restarts_from_the_notes_once(self):
        """Run 13: a coder re-read the same lines in circles and the whole round was lost."""
        def script(role, messages):
            if "conversation was compacted" in messages[1]["content"]:
                return [call("finish", summary="done after a fresh start", coverage=[])]
            return [call("search", pattern="add")]

        outcome, _ = self.run_agent(script)
        self.assertEqual((outcome.status, outcome.result["summary"]), ("done", "done after a fresh start"))

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

    def test_the_forced_last_step_keeps_the_finish_and_records_the_evidence_problem(self):
        """Run 17: a last-step finish was refused for lacking passing evidence, and its summary was lost with it."""
        plan = [[call("read_file", path="calc.py", start_line=n)] for n in range(1, 8)]
        plan.append([call("finish", summary="done as far as it goes", coverage=[])])
        outcome, _ = self.run_agent(plan, verify="false")
        self.assertEqual((outcome.status, outcome.steps), ("done", 8))
        self.assertEqual(outcome.result["summary"], "done as far as it goes")
        self.assertIn("the harness ran `false` for you", outcome.result["unresolved"])

    def test_coverage_must_cite_a_new_or_changed_test_for_each_criterion(self):
        """Run 17: #20's coder cited existing tests for the one criterion the maintainers' tests failed."""
        from graph_engineering.sandbox import Sandbox
        import shutil
        test, work = "python3 -m unittest discover -s tests -q", self.root / "box" / "work"
        shutil.copytree(self.repo, work)
        plan = [[call("finish", summary="s", evidence=[{"command": test, "exit_code": 0}],
                      coverage=[{"criterion": 1, "tests": "test_subtract"}, {"criterion": 2, "tests": "test_add (existing)"}])],
                [call("finish", summary="s", evidence=[{"command": test, "exit_code": 0}],
                      coverage=[{"criterion": 1, "tests": "test_subtract_negative"}, {"criterion": 2, "tests": "test_add"}],
                      unresolved="Criterion 1 is untested: it needs 2 mocked services.")],   # neither excuses criterion 2
                [call("finish", summary="s", evidence=[{"command": test, "exit_code": 0}],
                      coverage=[{"criterion": 1, "tests": "Test_Subtract"}, {"criterion": 2, "tests": "test_add"}],
                      unresolved="Criteria 1 and 2 are untested: they need the network.")]]
        plan = [[call("run", command=test)]] + plan
        llm = FakeLLM(lambda role, messages: plan[min(sum(m["role"] == "assistant" for m in messages), len(plan) - 1)])
        self.addCleanup(llm.close)
        agent = Agent(llm.model(), Workspace(work, Sandbox(self.root, work, trusted=True)), READ_WRITE, FINISH, "system",
                      evidence=True, max_steps=8, tests_changed=lambda: {"test_subtract"})
        outcome = agent.run("task")
        self.assertEqual(outcome.status, "done")
        refused = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"][1:3]
        for text in refused:
            self.assertIn("coverage for criteria 2 names no test this change adds or touches (those are: test_subtract)", text)
        self.assertEqual(outcome.result["coverage"][1]["tests"], "test_add")

    def test_the_third_scratch_file_counts_as_a_repeat_and_half_way_without_a_test_is_said(self):
        """Run 19: 29 probe scripts in one round, and two rounds of 80 steps that never changed a test."""
        import re
        from graph_engineering.repos import TEST_FILES
        steps = iter([[call("write_file", path="dbg.py", content="print(1)")], [call("write_file", path="dbg2.py", content="print(2)")],
                      [call("write_file", path="dbg.py", content="print(3)")],   # the same file again: still the third write
                      [call("read_file", path="calc.py")], [call("finish", summary="s", coverage=[])]])
        llm = FakeLLM(lambda role, messages: next(steps))
        self.addCleanup(llm.close)
        outcome = Agent(llm.model(), Workspace(self.repo), READ_WRITE[:5], FINISH, "s", max_steps=8,
                        is_test=lambda p: bool(re.search(TEST_FILES, p))).run("task")
        self.assertEqual(outcome.status, "done")
        sent = llm.requests[-1]["messages"]
        replies = [m["content"] for m in sent if m["role"] == "tool"]
        self.assertEqual(replies[1], "Created dbg2.py (1 lines)")
        self.assertTrue(replies[2].startswith("[this is scratch file write 3: probing with one-off scripts is not converging."))
        self.assertIn("write_file is not offered on your next step", replies[2])
        self.assertIn("Half your steps are spent and no test file has changed", [m["content"] for m in sent if m["role"] == "user"][-1])

    def test_an_added_file_that_looks_like_scratch_is_refused_once(self):
        """Run 18: a coder left backend/tests/__dbg-min.test.js behind and the gates held it to the red check."""
        steps = iter([[call("finish", summary="s", coverage=[])],
                      [call("finish", summary="s", coverage=[],
                            unresolved="__dbg-min.test.js and tmp_store.py are intended: they pin the minimal case")]])
        llm = FakeLLM(lambda role, messages: next(steps))
        self.addCleanup(llm.close)
        added = ["backend/tests/__dbg-min.test.js", "backend/context.js", "tests/tmp_store.py", "src/_private.py"]
        outcome = Agent(llm.model(), Workspace(self.repo), READ_WRITE[:3], FINISH, "s", added_files=lambda: added).run("task")
        self.assertEqual(outcome.status, "done")
        refused = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("added files look like scratch: backend/tests/__dbg-min.test.js, tests/tmp_store.py", refused)
        self.assertEqual(outcome.steps, 2)   # _private.py and context.js never counted; the named files pass
    def test_a_feedback_round_that_changes_nothing_needs_a_reason(self):
        """Run 17: after a rejected round, two rounds ran a hundred commands, changed nothing and finished "done"."""
        steps = iter([[call("finish", summary="s", coverage=[])],
                      [call("finish", summary="s", coverage=[], unresolved="the failing check is wrong: the test cannot fail on base")]])
        llm = FakeLLM(lambda role, messages: next(steps))
        self.addCleanup(llm.close)
        outcome = Agent(llm.model(), Workspace(self.repo), READ_WRITE[:3], FINISH, "s", must_change=True).run("task")
        self.assertEqual(outcome.status, "done")
        refused = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("nothing changed in this round although the previous attempt was rejected", refused)

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

    def test_an_adaptive_budget_thinks_only_at_the_start_and_after_a_failure(self):
        from graph_engineering.sandbox import Sandbox, available
        if not available():
            self.skipTest("no OS sandbox on this host")
        root = temp_dir(self)
        repo = python_repo(root)
        steps = iter([[call("read_file", path="calc.py")], [call("run", command="exit 1")], [call("run", command="true")],
                      [call("read_file", path="calc.py", start_line=1)], [call("finish", summary="s", coverage=[])]])
        llm = FakeLLM(lambda role, messages: next(steps))
        self.addCleanup(llm.close)
        model = llm.model()
        model.think = True
        Agent(model, Workspace(repo, Sandbox(root / "run", repo)), READ_WRITE, FINISH, "s", thinking_budget=-1024).run("task")
        thought = [r["chat_template_kwargs"]["enable_thinking"] for r in llm.requests]
        self.assertEqual(thought, [True, False, True, False, False])   # step 1; after `exit 1`; not after `true`
        self.assertEqual(llm.requests[2]["thinking_token_budget"], 1024)

    def test_failure_is_read_from_the_output_and_the_same_output_counts_as_a_repeat(self):
        """Run 16: `test | head -260; echo EXIT=$?` exits 0 while the test errors, and the coder ran it 77 times
        changing only the head count."""
        from graph_engineering.sandbox import Sandbox, available
        if not available():
            self.skipTest("no OS sandbox on this host")
        root = temp_dir(self)
        repo = python_repo(root)
        failing = "printf 'ERROR: test_x (t.T.test_x)\\nTraceback (most recent call last):\\n' | head -{n}; echo EXIT=$?"
        steps = iter([[call("read_file", path="calc.py")]] + [[call("run", command=failing.format(n=n))] for n in (5, 6)]
                     + [[call("finish", summary="s", coverage=[])]])
        llm = FakeLLM(lambda role, messages: next(steps))
        self.addCleanup(llm.close)
        model = llm.model()
        model.think = True
        outcome = Agent(model, Workspace(repo, Sandbox(root / "run", repo)), READ_WRITE, FINISH, "s",
                        thinking_budget=-1024).run("task")
        thought = [r["chat_template_kwargs"]["enable_thinking"] for r in llm.requests]
        self.assertEqual(thought[:3], [True, False, True])   # thinks after a command whose output shows a failure
        self.assertTrue(llm.requests[3]["messages"][-1]["content"].startswith("[the same output as this command gave at step 2"))
        self.assertEqual(outcome.status, "done")

    def test_an_adaptive_budget_also_thinks_after_a_repeat_but_not_merely_without_edits(self):
        root = temp_dir(self)
        repo = python_repo(root)
        reads = [[call("read_file", path="calc.py", start_line=n)] for n in (1, 2, 1, 1)]   # steps 3 and 4 repeat step 1
        reads += [[call("read_file", path="calc.py", start_line=n)] for n in (3, 4, 5, 6)]
        steps = iter(reads + [[call("finish", summary="s", coverage=[])]])
        llm = FakeLLM(lambda role, messages: next(steps))
        self.addCleanup(llm.close)
        model = llm.model()
        model.think = True
        Agent(model, Workspace(repo), READ_WRITE[:3], FINISH, "s", thinking_budget=-1024).run("task")
        thought = [r["chat_template_kwargs"]["enable_thinking"] for r in llm.requests]
        # step 1; steps 4 and 5 after the repeats; never for steps without an edit alone (run 18: 40% of thinking turns)
        self.assertEqual(thought, [True, False, False, True, True, False, False, False, False])

    def test_a_budget_of_zero_turns_thinking_off(self):
        llm = FakeLLM(lambda role, messages: [call("finish", summary="s", coverage=[])])
        self.addCleanup(llm.close)
        model = llm.model()
        model.think = True
        Agent(model, Workspace(python_repo(temp_dir(self))), READ_WRITE[:3], FINISH, "s", thinking_budget=0).run("task")
        self.assertFalse(llm.requests[0]["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("thinking_token_budget", llm.requests[0])
