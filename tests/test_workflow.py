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
        self.repos.save(Repo(name="calc", path=str(self.source), test_command=TEST_COMMAND, auto_accept=True, plan=False,
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
        self.assertEqual(kinds, ["created", "proposed", "accepted", "gates", "memory_updated", "ready", "published",
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

    def test_auto_planning_depends_on_ticket_size(self):
        repo = Repo(name="r", path="/tmp", test_command="t")
        self.assertFalse(repo.wants_plan({"acceptance_criteria": ["a", "b"]}))
        self.assertTrue(repo.wants_plan({"acceptance_criteria": ["a", "b", "c"]}))
        self.assertTrue(Repo(name="r", path="/tmp", test_command="t", plan=True).wants_plan({"acceptance_criteria": ["a"]}))

    def test_plan_is_checked_then_given_to_the_coder_and_the_reviewer(self):
        root = temp_dir(self)
        source = python_repo(root)
        home = root / "home"
        store, repos = Store(home / "ge.db"), Repos(home)
        repos.save(Repo(name="calc", path=str(source), test_command=TEST_COMMAND, plan=True,
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
