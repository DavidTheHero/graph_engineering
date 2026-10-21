"""Regressions for the independent review of the rebuild (functional findings)."""
import unittest

from graph_engineering import workflow
from graph_engineering.gates import run_gates
from graph_engineering.repos import Repo, Repos, detect
from graph_engineering.sandbox import Sandbox, available, prepare_workspace
from graph_engineering.store import Store

from helpers import SUB_SOURCE, SUB_TEST, TEST_COMMAND, FakeLLM, call, git, python_repo, temp_dir


@unittest.skipUnless(available(), "no OS sandbox on this host")
class GateFindings(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.source = python_repo(self.root)
        (self.source / ".gitignore").write_text("generated/\n")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-qm", "ignore generated")
        self.work = self.root / "run" / "work"
        self.base = prepare_workspace(self.source, "main", self.work)
        self.sandbox = Sandbox(self.root / "run", self.work)

    def gate(self, repo):
        report = run_gates(repo, self.work, self.base, self.sandbox, self.root / "run")
        return report, {c["name"]: c["ok"] for c in report["checks"]}

    def test_an_ignored_file_cannot_make_the_change_pass(self):
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST.replace("from calc import subtract", "from generated.ops import subtract"))
        (self.work / "generated").mkdir()
        (self.work / "generated" / "__init__.py").write_text("")
        (self.work / "generated" / "ops.py").write_text("def subtract(a, b):\n    return a - b\n")
        (self.work / "calc.py").write_text(SUB_SOURCE)
        repo = Repo(name="calc", path=str(self.source), test_command=TEST_COMMAND)
        report, checks = self.gate(repo)
        self.assertFalse(checks["test suite passes"], "the published commit has no generated/ directory")

    def test_an_unrelated_failure_on_base_is_not_red(self):
        (self.source / "tests" / "test_flaky.py").write_text(
            "import unittest\n\n\nclass Flaky(unittest.TestCase):\n    def test_flaky(self):\n        self.fail('broken on base')\n")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-qm", "broken test on base")
        root = self.root / "run2"
        work = root / "work"
        base = prepare_workspace(self.source, "main", work)
        (work / "tests" / "test_more.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass More(unittest.TestCase):\n    def test_more(self):\n        self.assertEqual(add(1, 1), 2)\n")
        (work / "calc.py").write_text(SUB_SOURCE)
        report = run_gates(Repo(name="calc", path=str(self.source), test_command=TEST_COMMAND), work, base, Sandbox(root, work), root)
        checks = {c["name"]: c["ok"] for c in report["checks"]}
        self.assertFalse(checks["new tests fail on base"])

    def test_gates_report_the_patch_they_tested(self):
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE)
        report, _ = self.gate(Repo(name="calc", path=str(self.source), test_command=TEST_COMMAND))
        self.assertTrue(report["passed"], report["checks"])
        patch = (self.root / "run" / "change.diff").read_text()
        self.assertIn("+def subtract(a, b):", patch)
        self.assertEqual(git(self.work, "rev-parse", f"{report['commit']}^{{tree}}").strip(), report["tree"])


class SmallFindings(unittest.TestCase):
    def test_pytest_runner_only_matches_test_modules(self):
        root = temp_dir(self)
        (root / "pyproject.toml").write_text("[project]\nname='x'\n[project.optional-dependencies]\ntest=['pytest']\n")
        repo = Repo(name="x", path=str(root), **{k: v for k, v in detect(root).items() if k in ("test_command", "test_file_runners")})
        self.assertIsNone(repo.runner("tests/conftest.py"))
        self.assertIsNone(repo.runner("tests/helpers.py"))
        self.assertEqual(repo.runner("tests/test_x y.py"), "python3 -m pytest -q 'tests/test_x y.py'")

    def test_model_prose_cannot_close_issues_or_mention_people(self):
        text = workflow.neutral("Fixes #12 and closes #3; thanks @octocat. Email a@b.c stays.")
        self.assertNotRegex(text, r"(?i)\b(fixes|closes)\s+#\d")
        self.assertNotIn("@octocat", text)

    def test_retry_never_shrinks_the_round_budget(self):
        store = Store(temp_dir(self) / "s.db")
        ticket = store.create("r", "x", "p", max_rounds=3, spec={"title": "t", "acceptance_criteria": ["a"]}, state="proposed")
        store.transition(ticket["id"], "accepted", "p", "accepted")
        store.transition(ticket["id"], "failed", "w", "step_failed", data={"failed_at": "develop"})
        self.assertEqual(workflow.retry(store, ticket["id"], "p")["max_rounds"], 3)


@unittest.skipUnless(available(), "no OS sandbox on this host")
class SetupRecovery(unittest.TestCase):
    def test_an_interrupted_setup_is_redone(self):
        root = temp_dir(self)
        source = python_repo(root)
        home = root / "home"
        store, repos = Store(home / "ge.db"), Repos(home)
        repos.save(Repo(name="calc", path=str(source), test_command=TEST_COMMAND, setup=["touch setup-ran"]))
        ticket = store.create("calc", "x", "p", spec={"title": "Add subtract", "problem": "p", "acceptance_criteria": ["a"]},
                              state="proposed")
        workflow.accept(store, repos, ticket["id"], "p")
        # A previous worker cloned the workspace and died before setup finished.
        prepare_workspace(source, "main", home / "work" / ticket["id"] / "work")
        llm = FakeLLM(lambda role, messages: [call("finish", summary="nothing", coverage=[])] if role == "coding"
                      else [call("submit_review", verdict="approve", findings=[], summary="ok")])
        self.addCleanup(llm.close)
        ctx = workflow.Context(home, store, repos, llm.registry(), owner="w")
        workflow.develop(ctx, store.claim("w", 600))
        self.assertTrue((home / "work" / ticket["id"] / "work" / "setup-ran").exists())
        self.assertTrue(store.get(ticket["id"])["data"]["setup_done"])


if __name__ == "__main__":
    unittest.main()
