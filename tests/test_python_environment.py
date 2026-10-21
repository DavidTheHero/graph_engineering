"""Declared Python environment for repository test runs (GE-30D05C40, 2026-10-21).

General_AI_Agent's Node tests start Python MCP servers needing pinned packages that live in the
repository's own .venv; the isolated snapshot ran the system python3 and 7 of 94 tests failed."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

from graph_engineering.control_repositories import inspect, register, set_python_environment
from graph_engineering.repository import python_interpreter, run_check, test_environment
from graph_engineering.tickets import Tickets
from tests.test_core import make_repo


@unittest.skipIf(os.name == "nt", "POSIX symlink and sh fixture")
class PythonEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.bin = root / "venv" / "bin"
        self.bin.mkdir(parents=True)
        self.python = self.bin / "python3"
        self.python.symlink_to(sys.executable)
        self.repo = make_repo(root)
        self.tickets = Tickets(root / "home")

    def tearDown(self):
        self.tickets.store.close()
        self.folder.cleanup()

    def test_declared_interpreter_is_first_on_path_for_test_runs(self):
        env = test_environment({"python_environment": str(self.python)})
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.bin))
        result = run_check(self.repo, ["sh", "-c", "command -v python3"], True, 30, env=env)
        self.assertEqual(result["output"].strip(), str(self.python))
        self.assertNotIn("VIRTUAL_ENV", test_environment({}))  # Unset: unchanged clean environment.

    def test_invalid_interpreters_are_refused(self):
        for value in ("python3", str(self.bin), str(self.bin / "node"), 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                python_interpreter(value)
        self.assertIsNone(python_interpreter(None))

    def test_set_python_environment_persists_is_inspectable_and_clearable(self):
        repo_id = register(self.tickets, str(self.repo))["id"]
        set_python_environment(self.tickets, repo_id, str(self.python))
        self.assertEqual(inspect(self.tickets, repo_id)["python_environment"], str(self.python))
        set_python_environment(self.tickets, repo_id, None)
        self.assertIsNone(inspect(self.tickets, repo_id)["python_environment"])
        with self.assertRaises(ValueError):
            set_python_environment(self.tickets, repo_id, str(self.bin / "missing-python"))


if __name__ == "__main__":
    unittest.main()
