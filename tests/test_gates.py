import unittest

from graph_engineering.gates import run_gates
from graph_engineering.repos import Repo
from graph_engineering.sandbox import Sandbox, available, prepare_workspace

from helpers import SUB_SOURCE, SUB_TEST, TEST_COMMAND, git, python_repo, temp_dir


@unittest.skipUnless(available(), "no OS sandbox on this host")
class Gates(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        source = python_repo(self.root)
        self.repo = Repo(name="calc", path=str(source), test_command=TEST_COMMAND,
                         test_file_runners=[{"pattern": r"^tests/.*\.py$", "command": "python3 -m unittest {file}"}])
        self.work = self.root / "run" / "work"
        self.base = prepare_workspace(source, "main", self.work)
        self.sandbox = Sandbox(self.root / "run", self.work)

    def gate(self, repo=None):
        report = run_gates(repo or self.repo, self.work, self.base, self.sandbox, self.root / "run")
        return report, {c["name"]: c["ok"] for c in report["checks"]}

    def broken_base(self):
        """A base whose suite already fails: every ticket on it would otherwise be refused."""
        source = self.root / "calc"
        (source / "tests" / "test_old.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass Old(unittest.TestCase):\n"
            "    def test_old(self):\n        self.assertEqual(add(1, 1), 3)\n")
        git(source, "add", "-A")
        git(source, "commit", "-q", "-m", "a failing test")
        self.work = self.root / "run2" / "work"
        self.base = prepare_workspace(source, "main", self.work)
        self.sandbox = Sandbox(self.root / "run2", self.work)
        repo = Repo(name="calc", path=str(source), test_command=TEST_COMMAND)
        return repo

    def test_failures_that_were_already_on_the_base_do_not_block(self):
        repo = self.broken_base()
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE)
        report = run_gates(repo, self.work, self.base, self.sandbox, self.root / "run2")
        checks = {c["name"]: c for c in report["checks"]}
        self.assertTrue(report["passed"], report["checks"])
        self.assertIn("already failed on the base", checks["test suite passes"]["detail"])
        self.assertIn("test_old", checks["test suite passes"]["detail"])

    def test_a_new_failure_on_a_failing_base_is_refused(self):
        repo = self.broken_base()
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE.replace("return a + b", "return a + b + 1"))
        report = run_gates(repo, self.work, self.base, self.sandbox, self.root / "run2")
        checks = {c["name"]: c for c in report["checks"]}
        self.assertFalse(checks["test suite passes"]["ok"])
        self.assertIn("New failures", checks["test suite passes"]["detail"])
        self.assertIn("test_add", checks["test suite passes"]["detail"])

    def test_a_real_fix_passes_every_gate(self):
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE)
        report, checks = self.gate()
        self.assertTrue(report["passed"], report["checks"])
        self.assertIn("tests/test_sub.py fails on base, passes on result", checks)

    def test_a_test_that_already_passes_on_base_is_refused(self):
        (self.work / "tests" / "test_more.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass More(unittest.TestCase):\n"
            "    def test_more(self):\n        self.assertEqual(add(1, 1), 2)\n")
        (self.work / "calc.py").write_text(SUB_SOURCE)
        report, checks = self.gate()
        self.assertFalse(report["passed"])
        self.assertFalse(checks["tests/test_more.py fails on base, passes on result"])

    def test_without_a_runner_the_whole_suite_must_fail_on_base(self):
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE)
        repo = Repo(name="calc", path=self.repo.path, test_command=TEST_COMMAND)
        report, checks = self.gate(repo)
        self.assertTrue(checks["new tests fail on base"], report["checks"])
        self.assertTrue(report["passed"])

    def test_source_only_and_deleted_tests_are_refused(self):
        (self.work / "calc.py").write_text(SUB_SOURCE)
        (self.work / "tests" / "test_add.py").unlink()
        report, checks = self.gate()
        self.assertFalse(checks["changes tests"] and checks["keeps existing tests"])
        self.assertFalse(report["passed"])

    def test_broken_suite_and_protected_paths_are_refused(self):
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / ".github").mkdir()
        (self.work / ".github" / "ci.yml").write_text("on: push\n")
        report, checks = self.gate()  # subtract is never implemented
        self.assertFalse(checks["test suite passes"])
        self.assertFalse(checks["leaves protected paths alone"])

    def test_test_configuration_is_guarded_unless_allowed(self):
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE)
        (self.work / "tests" / "conftest.py").write_text("collect_ignore = ['test_add.py']\n")
        _, checks = self.gate()
        self.assertFalse(checks["leaves test configuration alone"])
        report = run_gates(self.repo, self.work, self.base, self.sandbox, self.root / "run", allow_test_config=True)
        self.assertTrue({c["name"]: c["ok"] for c in report["checks"]}["leaves test configuration alone"])

    def test_package_test_scripts_are_guarded_but_other_scripts_are_not(self):
        import json
        (self.work / "package.json").write_text(json.dumps({"scripts": {"test": "node --test", "up": "node up.mjs"}}))
        from helpers import git as run_git
        run_git(self.work, "add", "-A")
        run_git(self.work, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "pkg")
        base = run_git(self.work, "rev-parse", "HEAD").strip()
        (self.work / "package.json").write_text(json.dumps({"scripts": {"test": "node --test", "up": "node up2.mjs", "dev": "x"}}))
        report = run_gates(self.repo, self.work, base, self.sandbox, self.root / "run")
        self.assertTrue({c["name"]: c["ok"] for c in report["checks"]}["leaves test configuration alone"])
        (self.work / "package.json").write_text(json.dumps({"scripts": {"test": "true", "up": "node up.mjs"}}))
        report = run_gates(self.repo, self.work, base, self.sandbox, self.root / "run")
        self.assertFalse({c["name"]: c["ok"] for c in report["checks"]}["leaves test configuration alone"])

    def test_generated_files_are_refused(self):
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE)
        (self.work / "data.db").write_text("x")
        _, checks = self.gate()
        self.assertFalse(checks["adds no generated or local files"])


if __name__ == "__main__":
    unittest.main()


class FailedTests(unittest.TestCase):
    def test_names_from_each_runner_and_none_when_unnamed(self):
        from graph_engineering.gates import failed_tests
        self.assertEqual(failed_tests("FAIL: test_x (tests.test_a.A.test_x)\nERROR: test_y (tests.test_a.A.test_y)\n"),
                         {"tests.test_a.A.test_x", "tests.test_a.A.test_y"})
        self.assertEqual(failed_tests("FAILED tests/test_a.py::test_x - assert 1 == 2\n"), {"tests/test_a.py::test_x"})
        self.assertEqual(failed_tests("\u2716 renders the model (12.5ms)\n\u2716 failing tests:\n\u2716 renders the model (12.9ms)\n"),
                         {"renders the model"})   # durations differ between runs
        self.assertIsNone(failed_tests("Segmentation fault\n"))

