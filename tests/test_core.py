import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from graph_engineering.runtime import Store, RepoLock, BusyError
from graph_engineering.repository import discover, Worktree, validate_edits, run_check


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def make_repo(root):
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@localhost")
    (repo / "calc.py").write_text("def add(a,b): return a-b\n")
    (repo / "test_calc.py").write_text("import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Introduce observable defect")
    return repo


class CoreTests(unittest.TestCase):
    def test_alternate_conventional_test_names_are_protected(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            git(repo, "mv", "test_calc.py", "testcalc.py")
            git(repo, "commit", "-m", "Use another standard unittest filename")
            info = discover(repo)
            self.assertIn("testcalc.py", info["test_files"])
            self.assertNotIn("testcalc.py", info["source_files"])
            for name in ("testcalc.py", "test.js", "foo_test.js", "foo-test.mjs", "thing.spec.ts"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    validate_edits(repo, [{"path": name, "content": "pass"}], [name])
    def test_unittest_adapter_discovers_tests_directory_without_package(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            (repo / "tests").mkdir()
            git(repo, "mv", "test_calc.py", "tests/test_calc.py")
            git(repo, "commit", "-m", "Use tests directory")
            info = discover(repo)
            result = run_check(repo, info["test_command"], trusted=True)
            self.assertFalse(result["empty_suite"])
            self.assertIn("test_add", result["output"])

    def test_checkpoint_and_effect_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root)
            run = store.create_run({"repo": str(root)}, run_id="test")
            counter = []
            result = store.effect(run["id"], "ticket", lambda: counter.append(1) or {"number": 7})
            store.checkpoint("test", "discovery", {"ticket": result}, "ticket_creation")
            store.close()
            store = Store(root)
            self.assertEqual(store.get("test")["state"]["ticket"]["number"], 7)
            self.assertEqual(store.effect("test", "ticket", lambda: self.fail("duplicate effect")), result)
            self.assertEqual(counter, [1])
            self.assertEqual(len(store.events("test")), 2)
            store.close()

    def test_conflicting_controller_and_path_protection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with RepoLock(root, "repo"):
                with self.assertRaises(BusyError):
                    with RepoLock(root, "repo"):
                        pass
            with RepoLock(root, "repo"):
                pass
            repo = make_repo(root)
            for path in ("../escape.py", ".git/config", ".github/workflows/a.yml", "test_calc.py", "AGENTS.md"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    validate_edits(repo, [{"path": path, "content": "malicious"}], ["calc.py"])

    def test_worktree_independent_frozen_regression(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            info = discover(repo)
            self.assertEqual(info["adapter"], "python")
            original = git(repo, "rev-parse", "HEAD")
            wt = Worktree(repo, root / "wt", "graph/test", original)
            wt.ensure()
            command = [os.sys.executable, "-m", "unittest", "discover", "-v"]
            before = run_check(wt.path, command, trusted=True)
            self.assertNotEqual(before["returncode"], 0)
            edits = validate_edits(wt.path, [{"path": "calc.py", "content": "def add(a,b): return a+b\n"}], ["calc.py"])
            wt.apply(edits)
            after = run_check(wt.path, command, trusted=True)
            self.assertEqual(after["returncode"], 0, after)
            self.assertEqual(git(repo, "rev-parse", "HEAD"), original)
            self.assertIn("return a-b", (repo / "calc.py").read_text())
            wt.remove()

    def test_untrusted_code_is_never_executed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            with self.assertRaises(PermissionError):
                run_check(path, [os.sys.executable, "-c", "open('owned','w').write('x')"], trusted=False)
            self.assertFalse((path / "owned").exists())

    def test_timeout_terminates_a_real_test_process(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_check(Path(td), [os.sys.executable, "-c", "import time; time.sleep(10)"], trusted=True, timeout=0.15)
            self.assertTrue(result["timed_out"])
            self.assertFalse(result["passed"])
            self.assertNotEqual(result["returncode"], 0)
            self.assertLess(result["duration_seconds"], 5)


if __name__ == "__main__":
    unittest.main()
