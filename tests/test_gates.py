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

    def test_an_earlier_snapshot_can_be_judged_instead_of_the_work_tree(self):
        from graph_engineering.gates import snapshot
        (self.work / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.work / "calc.py").write_text(SUB_SOURCE)
        green = snapshot(self.work, self.base)
        (self.work / "calc.py").write_text("def add(a, b):\n    return a + b\n")   # broken again
        report, checks = self.gate()
        self.assertFalse(report["passed"])
        report = run_gates(self.repo, self.work, self.base, self.sandbox, self.root / "run" / "green", commit=green)
        self.assertTrue(report["passed"], [c for c in report["checks"] if not c["ok"]])
        self.assertEqual(report["commit"], green)

    def test_a_test_that_already_passes_on_base_is_refused(self):
        (self.work / "tests" / "test_more.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass More(unittest.TestCase):\n"
            "    def test_more(self):\n        self.assertEqual(add(1, 1), 2)\n")
        (self.work / "calc.py").write_text(SUB_SOURCE)
        report, checks = self.gate()
        self.assertFalse(report["passed"])
        self.assertFalse(checks["tests/test_more.py fails on base, passes on result"])
        detail = [c["detail"] for c in report["checks"] if c["name"] == "tests/test_more.py fails on base, passes on result"][0]
        self.assertTrue(detail.startswith("The tests you changed in tests/test_more.py pass on the base, without your source "
                                          "change, so they do not detect the behaviour they are meant to. Changed tests: test_more."))

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


class ChangedTests(unittest.TestCase):
    def test_a_test_counts_as_changed_when_a_line_inside_it_is(self):
        from graph_engineering.gates import changed_tests
        root = temp_dir(self)
        repo = python_repo(root)
        (repo / "tests" / "test_add.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass Add(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
            "    def test_add_negative(self):\n        self.assertEqual(add(-2, 3), 1)\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "two tests")
        base = git(repo, "rev-parse", "HEAD").strip()
        is_test = Repo(name="calc", path=str(repo), test_command=TEST_COMMAND).is_test
        self.assertEqual(changed_tests(repo, base, is_test), set())
        (repo / "tests" / "test_add.py").write_text((repo / "tests" / "test_add.py").read_text().replace("add(-2, 3), 1", "add(-2, 3), 1)  # changed"))
        (repo / "tests" / "test_sub.py").write_text(SUB_TEST)
        (repo / "tests" / "test_sub.spec.js").write_text("import test from 'node:test';\ntest('subtracts two numbers', () => {});\n"
                                                        "it(\"it form\", () => {});\n")
        (repo / "calc.py").write_text(SUB_SOURCE)   # a source change is not a test
        self.assertEqual(changed_tests(repo, base, is_test),
                         {"test_add_negative", "test_subtract", "subtracts two numbers", "it form"})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "more")
        base = git(repo, "rev-parse", "HEAD").strip()
        text = (repo / "tests" / "test_add.py").read_text()
        (repo / "tests" / "test_add.py").write_text(text[:text.index("    def test_add_negative")])   # a deletion only
        self.assertEqual(changed_tests(repo, base, is_test), set())
        (repo / "tests" / "test_link.py").symlink_to(repo / "tests" / "test_sub.py")   # never read through a link
        self.assertEqual(changed_tests(repo, base, is_test), set())


class Staging(unittest.TestCase):
    def test_a_locked_or_corrupt_index_left_by_the_sandbox_does_not_stop_staging(self):
        from graph_engineering.gates import snapshot
        root = temp_dir(self)
        repo = python_repo(root)
        base = git(repo, "rev-parse", "HEAD").strip()
        (repo / "new.py").write_text("x = 1\n")
        (repo / ".git" / "index.lock").write_text("")
        commit = snapshot(repo, base)
        self.assertIn("new.py", git(repo, "diff", "--name-only", base, commit))
        (repo / ".git" / "index").write_text("GARBAGE")
        (repo / "new.py").write_text("x = 2\n")
        commit = snapshot(repo, base)
        self.assertIn("x = 2", git(repo, "show", f"{commit}:new.py"))


class Feedback(unittest.TestCase):
    def test_failures_put_what_failed_before_the_full_output(self):
        from graph_engineering.gates import failures
        detail = "\n".join(["✔ passes %d (1ms)" % n for n in range(40)]
                            + ["✖ daily research submits one job (3ms)", "AssertionError: 0 !== 1", "    at Test.run"])
        report = {"checks": [{"name": "changes tests", "ok": True, "detail": ""},
                             {"name": "test suite passes", "ok": False, "detail": detail}]}
        text = failures(report)
        self.assertTrue(text.startswith("FAILED: test suite passes\nWhat failed:\n✖ daily research submits one job (3ms)\n"
                                        "AssertionError: 0 !== 1\n\nFull output:\n✔ passes 0"))
        self.assertEqual(failures({"checks": [{"name": "x", "ok": False, "detail": "no clue"}]}), "FAILED: x\nno clue")
