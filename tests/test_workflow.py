import unittest

from graph_engineering import workflow
from graph_engineering.repos import Repo, Repos
from graph_engineering.sandbox import available
from graph_engineering.store import Conflict, Store

from helpers import SUB_SOURCE, SUB_TEST, TEST_COMMAND, FakeLLM, call, git, python_repo, temp_dir

SPEC = {"verdict": "ready", "title": "Add subtract", "kind": "feat", "problem": "calc has no subtract",
        "acceptance_criteria": ["subtract(a, b) returns a - b"], "reason": "clear"}


def turns(messages):
    return sum(1 for m in messages if m["role"] == "assistant")


def coder(good=True):
    def step(messages):
        plan = [[call("write_file", path="tests/test_sub.py", content=SUB_TEST)],
                [call("write_file", path="calc.py", content=SUB_SOURCE if good else "def add(a, b):\n    return a + b\n")],
                [call("run", command=TEST_COMMAND)],
                [call("finish", summary="Added subtract with a test", coverage=[{"criterion": 1, "tests": "test_subtract"}],
                      **({"evidence": [{"command": TEST_COMMAND, "exit_code": 0}]} if good else
                         {"evidence": [{"command": TEST_COMMAND, "exit_code": 1}], "unresolved": "subtract still fails"}))]]
        return plan[min(turns(messages), 3)]
    return step


class Store_(unittest.TestCase):
    def setUp(self):
        self.store = Store(temp_dir(self) / "s.db")

    def test_only_declared_transitions_are_allowed(self):
        ticket = self.store.create("calc", "do it", "person")
        with self.assertRaises(Conflict):
            self.store.transition(ticket["id"], "done", "person", "skip")
        self.store.transition(ticket["id"], "proposed", "w", "proposed")
        self.assertEqual([e["kind"] for e in self.store.events(ticket["id"])], ["created", "proposed"])

    def test_a_person_cannot_act_on_a_leased_ticket_except_to_cancel(self):
        ticket = self.store.create("calc", "do it", "person", spec={"title": "t", "acceptance_criteria": ["a"]}, state="proposed")
        self.store.transition(ticket["id"], "accepted", "person", "accepted")
        self.assertEqual(self.store.claim("worker-1", 60)["id"], ticket["id"])
        self.assertIsNone(self.store.claim("worker-2", 60))
        with self.assertRaises(Conflict):
            self.store.transition(ticket["id"], "ready", "person", "sneak")
        self.assertEqual(self.store.transition(ticket["id"], "cancelled", "person", "cancelled")["state"], "cancelled")

    def test_ready_waits_for_a_person_unless_publication_is_automatic(self):
        ticket = self.store.create("calc", "x", "p", spec={"title": "t", "acceptance_criteria": ["a"]}, state="proposed")
        self.store.transition(ticket["id"], "accepted", "p", "accepted")
        self.store.transition(ticket["id"], "ready", "p", "ready", data={"publish": False})
        self.assertIsNone(self.store.claim("w", 60))
        workflow.approve_publish(self.store, ticket["id"], "person")
        self.assertEqual(self.store.claim("w", 60)["id"], ticket["id"])


@unittest.skipUnless(available(), "no OS sandbox on this host")
class Workflow(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.source = python_repo(self.root)
        self.home = self.root / "home"
        self.store, self.repos = Store(self.home / "ge.db"), Repos(self.home)
        self.repos.save(Repo(name="calc", path=str(self.source), test_command=TEST_COMMAND, auto_accept=True, plan=False, profile="thorough",
                             acceptance_tests=False,
                             test_file_runners=[{"pattern": r"^tests/.*\.py$", "command": "python3 -m unittest {file}"}]))

    def context(self, script):
        llm = FakeLLM(script)
        self.addCleanup(llm.close)
        self.llm = llm
        return workflow.Context(self.home, self.store, self.repos, llm.registry())

    def drive(self, ctx, until, limit=12):
        """Run steps as the scheduler would until the ticket reaches a state in ``until``."""
        for _ in range(limit):
            ticket = self.store.claim(ctx.owner, 600)
            if ticket is None:
                break
            workflow.advance(ctx, ticket)
            current = self.store.get(ticket["id"])
            if current["lease_owner"]:
                self.store.release(ticket["id"], ctx.owner)
            if current["state"] in until:
                return current
        return self.store.list()[0]

    def test_request_to_merged_commit(self):
        reviews = []

        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "review":
                reviews.append(messages[1]["content"])
                return [call("submit_review", verdict="approve", findings=[], summary="Correct and tested.")]
            if role == "memory":
                return [call("submit_memory", content="- tests: python3 -m unittest discover -s tests -q")]
            return coder()(messages)

        ctx = self.context(script)
        ticket = self.store.create("calc", "Please add a subtract function", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"))
        self.assertEqual(ticket["state"], "awaiting_merge", ticket["error"])
        self.assertIn("PASS tests/test_sub.py fails on base, passes on result", reviews[0])
        branch = ticket["data"]["pr"]["branch"]
        self.assertEqual(git(self.source, "rev-parse", branch).strip(), ticket["data"]["head"])
        self.assertNotIn("subtract", (self.source / "calc.py").read_text())  # nothing merged without approval

        with self.assertRaises(Conflict):
            workflow.approve_merge(self.store, ticket["id"], "person", "0" * 40)
        workflow.approve_merge(self.store, ticket["id"], "person", ticket["data"]["head"])
        ticket = self.drive(ctx, ("done",))
        self.assertEqual(ticket["state"], "done", ticket["error"])
        self.assertIn("def subtract", (self.source / "calc.py").read_text())
        kinds = [e["kind"] for e in self.store.events(ticket["id"]) if e["kind"] != "agent"]
        self.assertEqual(kinds, ["created", "proposed", "accepted", "green", "gates", "memory_updated", "ready", "published",
                                 "merge_approved", "merged"])
        self.assertIn("python3 -m unittest discover", workflow.read_memory(ctx, "calc"))

    def test_memory_opens_the_next_ticket_and_a_failing_librarian_never_blocks(self):
        openings = []

        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "review":
                return [call("submit_review", verdict="approve", findings=[], summary="ok")]
            if role == "memory":
                return "I will not call the tool."   # stalls: the librarian fails
            if sum(m["role"] == "assistant" for m in messages) == 0:
                openings.append(messages[1]["content"])
            return coder()(messages)

        ctx = self.context(script)
        path = workflow.memory_path(ctx, "calc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("- REMEMBERED: tests live in tests/\n")
        self.store.create("calc", "Please add subtract", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"))
        self.assertEqual(ticket["state"], "awaiting_merge", ticket["error"])
        self.assertIn("REMEMBERED: tests live in tests/", openings[0])
        self.assertIn("memory_skipped", [e["kind"] for e in self.store.events(ticket["id"])])
        self.assertEqual(path.read_text(), "- REMEMBERED: tests live in tests/\n")

    def test_a_failed_round_restarts_from_its_last_green_state(self):
        """Run 20: a coder had the suite green, then added tests it could not satisfy and the round was lost."""
        feedback_seen = []
        broken = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a + b\n"

        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "review":
                return [call("submit_review", verdict="approve", findings=[], summary="ok")]
            if turns(messages) == 0:
                feedback_seen.append(messages[1]["content"])
            if len(feedback_seen) == 1:   # round 1: green at step 3, then broken
                plan = [[call("write_file", path="tests/test_sub.py", content=SUB_TEST)],
                        [call("write_file", path="calc.py", content=SUB_SOURCE)],
                        [call("run", command=TEST_COMMAND)],
                        [call("write_file", path="calc.py", content=broken)],
                        [call("run", command=TEST_COMMAND)],
                        [call("finish", summary="tried", coverage=[{"criterion": 1, "tests": "test_subtract"}],
                              evidence=[{"command": TEST_COMMAND, "exit_code": 1}], unresolved="subtract fails")]]
                return plan[min(turns(messages), 5)]
            plan = [[call("run", command=TEST_COMMAND)],
                    [call("finish", summary="the restored state is complete", coverage=[{"criterion": 1, "tests": "test_subtract"}],
                          evidence=[{"command": TEST_COMMAND, "exit_code": 0}], unresolved="nothing to change: the restored state passes")]]
            return plan[min(turns(messages), 1)]

        ctx = self.context(script)
        self.store.create("calc", "Please add subtract", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"))
        self.assertEqual(ticket["state"], "awaiting_merge", ticket["error"])
        self.assertEqual(ticket["round"], 2)
        self.assertIn("At step 3 of your previous attempt the whole suite passed", feedback_seen[1])
        self.assertIn("return a + b\n", feedback_seen[1])   # the later edit is attached
        kinds = [e["kind"] for e in self.store.events(ticket["id"])]
        self.assertIn("green", kinds)
        self.assertIn("restored_green", kinds)

    def test_failed_gates_feed_the_next_round(self):
        prompts_seen = []

        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "review":
                return [call("submit_review", verdict="approve", findings=[], summary="ok")]
            if turns(messages) == 0:
                prompts_seen.append(messages[1]["content"])
            return coder(good=len(prompts_seen) > 1)(messages)

        ctx = self.context(script)
        self.store.create("calc", "Please add subtract", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"))
        self.assertEqual(ticket["state"], "awaiting_merge", ticket["error"])
        self.assertEqual(ticket["round"], 2)
        self.assertNotIn("<feedback>", prompts_seen[0])
        self.assertIn("FAILED: test suite passes", prompts_seen[1])

    def test_review_changes_then_budget_spent_then_retry(self):
        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "review":
                return [call("submit_review", verdict="changes", summary="no",
                             findings=[{"file": "calc.py", "problem": "missing docstring", "fix": "add one"}])]
            return coder()(messages)

        ctx = self.context(script)
        ticket = self.store.create("calc", "Please add subtract", "person")
        self.store.transition(ticket["id"], "new", "person", "noop", max_rounds=2) if False else None
        ticket = self.drive(ctx, ("failed",), limit=10)
        self.assertEqual(ticket["state"], "failed")
        self.assertEqual(ticket["round"], 3)
        self.assertIn("missing docstring", ticket["data"]["feedback"])
        ticket = workflow.retry(self.store, ticket["id"], "person")
        self.assertEqual((ticket["state"], ticket["max_rounds"]), ("accepted", 4))

    def test_notes_without_findings_do_not_block_and_reach_the_pr(self):
        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "review":
                return [call("submit_review", verdict="changes", findings=[], notes=["shutdown waits 10 s on spawn failure"],
                             summary="works")]
            return coder()(messages)

        ctx = self.context(script)
        self.store.create("calc", "Please add subtract", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"), limit=6)
        self.assertEqual((ticket["state"], ticket["round"]), ("awaiting_merge", 1), ticket["error"])
        body = workflow.pr_body(ticket, 7)
        self.assertIn("Reviewer notes (not blocking):\n- shutdown waits 10 s on spawn failure", body)

    def test_quick_profile_skips_plan_and_review_and_says_so_in_the_pr(self):
        roles = []

        def script(role, messages):
            roles.append(role)
            if role == "intake":
                return [call("submit_ticket", **{**SPEC, "acceptance_criteria": ["a", "b", "c"]})]
            if role == "memory":
                return [call("submit_memory", content="- guide")]
            return coder()(messages)

        repo = self.repos.get("calc")
        from dataclasses import replace
        self.repos.save(replace(repo, profile="quick", plan=True))
        ctx = self.context(script)
        self.store.create("calc", "Please add subtract", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"))
        self.assertEqual(ticket["state"], "awaiting_merge", ticket["error"])
        self.assertNotIn("plan", roles)
        self.assertNotIn("review", roles)
        self.assertIn("Quick profile: no model review", workflow.pr_body(ticket, 3))

    def test_a_reproduced_probe_failure_sends_the_change_back_once(self):
        probe_test = ("import unittest\nfrom calc import subtract\n\n\nclass Probe(unittest.TestCase):\n"
                      "    def test_floats(self):\n        self.assertEqual(subtract(0.5, 0.25), 1)\n")
        passing = probe_test.replace("Probe", "Fine").replace("1)", "0.25)")
        probes, coding_tasks = [], []

        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "memory":
                return [call("submit_memory", content="- guide")]
            if role == "probe":
                if turns(messages) == 0:
                    probes.append(messages[1]["content"])
                steps = [[call("write_file", path="tests/test_probe_sub.py", content=probe_test),
                          call("write_file", path="tests/test_probe_ok.py", content=passing)],
                         [call("submit_probes", probes=[
                             {"path": "tests/test_probe_sub.py", "test": "test_floats", "requirement": "subtract(a, b)",
                              "observed": "0.25"},
                             {"path": "tests/test_probe_ok.py", "test": "test_floats", "requirement": "x", "observed": "y"},
                             {"path": "../outside.py", "test": "t", "requirement": "x", "observed": "y"}])]]
                return steps[min(turns(messages), 1)]
            if turns(messages) == 0:
                coding_tasks.append(messages[1]["content"])
            return coder()(messages)

        from dataclasses import replace
        self.repos.save(replace(self.repos.get("calc"), profile="quick", probe=True))
        ctx = self.context(script)
        self.store.create("calc", "Please add subtract", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"), limit=20)
        self.assertEqual(ticket["state"], "awaiting_merge", ticket["error"])
        self.assertEqual(len(probes), 1)                        # once per ticket
        self.assertEqual(len(coding_tasks), 2)
        self.assertIn("tests/test_probe_sub.py :: test_floats", coding_tasks[1])
        self.assertIn("subtract(0.5, 0.25), 1", coding_tasks[1])   # the test itself reaches the coder
        self.assertNotIn("test_probe_ok", coding_tasks[1])        # a reported probe that passes is not a finding
        self.assertNotIn("outside", coding_tasks[1])
        event = next(e for e in self.store.events(ticket["id"]) if e["kind"] == "probed")
        self.assertEqual(event["data"]["reproduced"], ["tests/test_probe_sub.py"])

    def test_a_probe_fix_that_breaks_the_gates_falls_back_to_the_gated_result(self):
        """Run 12: answering a probe broke the gates in the last round, and a result that had passed was lost."""
        probe_test = ("import unittest\nfrom calc import subtract\n\n\nclass Probe(unittest.TestCase):\n"
                      "    def test_floats(self):\n        self.assertEqual(subtract(0.5, 0.25), 1)\n")

        def script(role, messages):
            if role == "intake":
                return [call("submit_ticket", **SPEC)]
            if role == "memory":
                return [call("submit_memory", content="- guide")]
            if role == "probe":
                steps = [[call("write_file", path="tests/test_probe_sub.py", content=probe_test)],
                         [call("submit_probes", probes=[{"path": "tests/test_probe_sub.py", "test": "test_floats",
                                                          "requirement": "subtract(a, b)", "observed": "0.25"}])]]
                return steps[min(turns(messages), 1)]
            if "A tester wrote tests" in messages[1]["content"]:   # the answer to the probe breaks add
                steps = [[call("write_file", path="calc.py", content="def subtract(a, b):\n    return 1\n")],
                         [call("finish", summary="Changed subtract", coverage=[],
                               evidence=[{"command": TEST_COMMAND, "exit_code": 1}], unresolved="add broke")]]
                return steps[min(turns(messages), 1)]
            return coder()(messages)

        from dataclasses import replace
        self.repos.save(replace(self.repos.get("calc"), profile="quick", probe=True))
        ctx = self.context(script)
        self.store.create("calc", "Please add subtract", "person")
        ticket = self.drive(ctx, ("awaiting_merge", "failed"), limit=20)
        self.assertEqual(ticket["state"], "awaiting_merge", ticket["error"])
        self.assertEqual(git(self.source, "show", f"{ticket['data']['head']}:calc.py"), SUB_SOURCE)
        self.assertIn("tests/test_probe_sub.py :: test_floats", workflow.pr_body(ticket, 3))
        self.assertIn("probe_fallback", [e["kind"] for e in self.store.events(ticket["id"])])

    def test_needs_info_and_answer(self):
        def script(role, messages):
            if "Clarification from the person" in messages[1]["content"]:
                return [call("submit_ticket", **{**SPEC, "verdict": "not_actionable", "reason": "already exists"})]
            return [call("submit_ticket", **{**SPEC, "verdict": "needs_info", "questions": ["Which module?"]})]

        ctx = self.context(script)
        ticket = self.store.create("calc", "make it better", "person")
        ticket = self.drive(ctx, ("needs_info",))
        self.assertEqual(ticket["state"], "needs_info")
        workflow.answer(self.store, ticket["id"], "person", "calc.py")
        self.assertEqual(self.drive(ctx, ("rejected",))["state"], "rejected")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(available(), "no OS sandbox on this host")
class Planning(unittest.TestCase):
    def test_unresolved_plan_concerns_become_the_final_reviews_checklist(self):
        from graph_engineering import prompts
        text = prompts.review_task({"title": "t", "problem": "p", "acceptance_criteria": ["a"]}, "diff", "- PASS x", "s",
                                   concerns="- chains.model is undefined with fakeChains")
        self.assertIn("any that the code does not resolve is a blocking finding", text)
        self.assertIn("chains.model is undefined with fakeChains", text)

    def test_larger_tickets_get_more_coding_steps(self):
        repo = Repo(name="r", path="/tmp", test_command="t")
        self.assertEqual(repo.coding_steps_for({"acceptance_criteria": ["a"] * 4}), 80)
        self.assertEqual(repo.coding_steps_for({"acceptance_criteria": ["a"] * 5}), 100)
        self.assertEqual(repo.coding_steps_for({"acceptance_criteria": ["a"] * 9}), 120)

    def test_auto_planning_depends_on_ticket_size(self):
        repo = Repo(name="r", path="/tmp", test_command="t", profile="thorough")
        self.assertFalse(Repo(name="r", path="/tmp", test_command="t", plan=True).wants_plan({"acceptance_criteria": ["a"] * 5}))
        self.assertFalse(repo.wants_plan({"acceptance_criteria": ["a", "b"]}))
        self.assertTrue(repo.wants_plan({"acceptance_criteria": ["a", "b", "c"]}))
        self.assertTrue(Repo(name="r", path="/tmp", test_command="t", plan=True, profile="thorough").wants_plan({"acceptance_criteria": ["a"]}))

    def test_plan_is_checked_then_given_to_the_coder_and_the_reviewer(self):
        root = temp_dir(self)
        source = python_repo(root)
        home = root / "home"
        store, repos = Store(home / "ge.db"), Repos(home)
        repos.save(Repo(name="calc", path=str(source), test_command=TEST_COMMAND, plan=True, acceptance_tests=False, profile="thorough",
                        test_file_runners=[{"pattern": r"^tests/.*\.py$", "command": "python3 -m unittest {file}"}]))
        seen = {"plans": 0, "coder": None, "review": None, "plan_feedback": None}
        good_plan = {"approach": "add subtract beside add", "files": [{"path": "calc.py", "change": "add subtract"}],
                     "tests": [{"criterion": 1, "file": "tests/test_sub.py", "checks": "subtract(5, 3) == 2"}]}

        def script(role, messages):
            if role == "plan":
                seen["plans"] += 1
                if seen["plans"] == 1:
                    return [call("submit_plan", approach="x", files=[], tests=[])]   # covers no criterion
                seen["plan_feedback"] = messages[1]["content"]
                return [call("submit_plan", **good_plan)]
            if role == "plan_review":
                return [call("submit_plan_review", verdict="approve", problems=[], summary="covers it")]
            if role == "review":
                seen["review"] = messages[1]["content"]
                return [call("submit_review", verdict="approve", findings=[], summary="ok")]
            if seen["coder"] is None:
                seen["coder"] = messages[1]["content"]
            return coder()(messages)

        llm = FakeLLM(script)
        self.addCleanup(llm.close)
        ctx = workflow.Context(home, store, repos, llm.registry(), owner="w")
        ticket = store.create("calc", "x", "p", spec={k: SPEC[k] for k in ("title", "kind", "problem", "acceptance_criteria")},
                              state="proposed")
        workflow.accept(store, repos, ticket["id"], "p")
        workflow.develop(ctx, store.claim("w", 600))
        ticket = store.get(ticket["id"])
        self.assertEqual(ticket["state"], "ready", ticket["error"])
        self.assertIn("Criteria 1 have no test in the plan", seen["plan_feedback"])
        self.assertIn("<plan>", seen["coder"])
        self.assertIn("subtract(5, 3) == 2", seen["coder"])
        self.assertIn("The agreed plan", seen["review"])
        self.assertTrue(ticket["data"]["plan_record"]["approved"])


@unittest.skipUnless(available(), "no OS sandbox on this host")
class AcceptanceTests(unittest.TestCase):
    def test_written_before_coding_kept_only_if_red_and_edits_reach_the_reviewer(self):
        root = temp_dir(self)
        source = python_repo(root)
        home = root / "home"
        store, repos = Store(home / "ge.db"), Repos(home)
        repos.save(Repo(name="calc", path=str(source), test_command=TEST_COMMAND, plan=False, profile="thorough",
                        test_file_runners=[{"pattern": r"^tests/test_[^/]*\.py$", "command": "python3 -m unittest {file}"}]))
        seen = {}
        already_green = "import unittest\nfrom calc import add\n\n\nclass G(unittest.TestCase):\n    def test_g(self):\n        self.assertEqual(add(1, 1), 2)\n"

        def script(role, messages):
            turn = sum(m["role"] == "assistant" for m in messages)
            if role == "acceptance":
                return [[call("write_file", path="tests/test_sub.py", content=SUB_TEST)],
                        [call("write_file", path="calc.py", content="def add(a, b):\n    return 0\n")],   # not allowed: undone
                        [call("write_file", path="tests/test_green.py", content=already_green)],          # passes now: dropped
                        [call("submit_tests", tests=[{"path": "tests/test_sub.py", "criterion": 1, "checks": "5-3"}])]][turn]
            if role == "review":
                seen["review"] = messages[1]["content"]
                return [call("submit_review", verdict="approve", findings=[], summary="ok")]
            if role == "memory":
                return [call("submit_memory", content="- guide")]
            if turn == 0:
                seen["coder"] = messages[1]["content"]
            return [[call("write_file", path="calc.py", content=SUB_SOURCE)],
                    [call("write_file", path="tests/test_sub.py", content=SUB_TEST + "\n# tidied\n")],
                    [call("run", command=TEST_COMMAND)],
                    [call("finish", summary="done", coverage=[], evidence=[{"command": TEST_COMMAND, "exit_code": 0}])]][min(turn, 3)]

        llm = FakeLLM(script)
        self.addCleanup(llm.close)
        ctx = workflow.Context(home, store, repos, llm.registry(), owner="w")
        ticket = store.create("calc", "x", "p", spec={k: SPEC[k] for k in ("title", "kind", "problem", "acceptance_criteria")},
                              state="proposed")
        workflow.accept(store, repos, ticket["id"], "p")
        workflow.develop(ctx, store.claim("w", 600))
        ticket = store.get(ticket["id"])
        self.assertEqual(ticket["state"], "ready", ticket["error"])
        acceptance = ticket["data"]["acceptance"]
        self.assertEqual(list(acceptance["files"]), ["tests/test_sub.py"])
        self.assertEqual(acceptance["dropped"], ["tests/test_green.py"])
        self.assertIn("you may not change them (the harness checks): tests/test_sub.py", seen["coder"])
        self.assertIn("tests/test_sub.py was changed or removed by the coder", seen["review"])
