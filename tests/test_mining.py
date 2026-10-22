import unittest

from graph_engineering.mining import candidates, criteria_from, task_for, validate
from graph_engineering.repos import Repo
from graph_engineering.sandbox import available

from helpers import SUB_SOURCE, SUB_TEST, TEST_COMMAND, git, python_repo, temp_dir


class Mining(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.source = python_repo(self.root)
        (self.source / "tests" / "test_sub.py").write_text(SUB_TEST)
        (self.source / "calc.py").write_text(SUB_SOURCE)
        git(self.source, "add", "-A")
        git(self.source, "commit", "-qm", "Add subtract; keep add unchanged\n\n- subtract(a, b) returns a - b\n- add still works")
        (self.source / "tests" / "test_add.py").write_text((self.source / "tests" / "test_add.py").read_text() + "\n")
        git(self.source, "commit", "-qam", "Tidy a test")  # tests only: not a task
        self.repo = Repo(name="calc", path=str(self.source), test_command=TEST_COMMAND,
                         test_file_runners=[{"pattern": r"^tests/test_[^/]*\.py$", "command": "python3 -m unittest {file}"}])

    def test_only_commits_changing_tests_and_source_are_candidates(self):
        found = candidates(self.repo, "main", 10)
        self.assertEqual(len(found), 1)
        task = task_for(self.repo, found[0])
        self.assertEqual(task["spec"]["acceptance_criteria"], ["subtract(a, b) returns a - b", "add still works"])
        self.assertEqual(task["hidden_tests"]["command"], "python3 -m unittest tests/test_sub.py")
        self.assertTrue(task["base"].endswith("^"))

    @unittest.skipUnless(available(), "no OS sandbox on this host")
    def test_a_task_is_kept_only_if_the_reference_solution_validates(self):
        task = task_for(self.repo, candidates(self.repo, "main", 10)[0])
        self.assertTrue(validate(self.repo, task, self.root / "v")["valid"])
        broken = {**task, "hidden_tests": {**task["hidden_tests"], "command": "python3 -m unittest tests/test_add.py"}}
        verdict = validate(self.repo, broken, self.root / "v")
        self.assertEqual((verdict["valid"], verdict["reason"]), (False, "the hidden tests already pass on the parent"))

    def test_criteria_from_an_acceptance_section(self):
        text = "Problem text.\n\n## Acceptance criteria\nOne thing works. Another thing works.\n\n## Validation\nnpm test"
        self.assertEqual(criteria_from(text), ["One thing works.", "Another thing works."])
        self.assertEqual(criteria_from("Intro\n\nAcceptance:\n- first\n- second"), ["first", "second"])


if __name__ == "__main__":
    unittest.main()
