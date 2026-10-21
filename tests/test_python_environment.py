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

    def test_virtual_env_is_set_only_for_a_real_virtual_environment(self):
        self.assertNotIn("VIRTUAL_ENV", test_environment({"python_environment": str(self.python)}))  # No pyvenv.cfg.
        (self.bin.parent / "pyvenv.cfg").write_text("home = /usr/bin\n")
        self.assertEqual(test_environment({"python_environment": str(self.python)})["VIRTUAL_ENV"], str(self.bin.parent))

    def test_interpreter_probe_receives_no_controller_secrets(self):
        """Review of PR #5: the repository's .venv startup hooks must not see controller credentials."""
        seen = Path(self.folder.name) / "seen-env.txt"
        wrapper = self.bin / "python3.12"
        wrapper.write_text(f"#!/bin/sh\nenv > '{seen}'\nexec '{sys.executable}' \"$@\"\n")
        wrapper.chmod(0o755)
        repo_id = register(self.tickets, str(self.repo))["id"]
        os.environ["GITHUB_TOKEN"] = "ghp_controller_secret_value"
        try:
            set_python_environment(self.tickets, repo_id, str(wrapper))
        finally:
            del os.environ["GITHUB_TOKEN"]
        self.assertNotIn("ghp_controller_secret_value", seen.read_text())

    def test_parallel_quality_uses_the_declared_environment(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from graph_engineering import quality
        seen = []

        def fake(path, argv, trusted, timeout, cancelled, env=None):
            seen.append(env)
            return {"passed": True, "returncode": 0, "timed_out": False, "empty_suite": False, "output": ""}
        store = SimpleNamespace(event=lambda *a, **k: None, artifact=lambda *a, **k: "artifact")
        flow = SimpleNamespace(run_id="r", store=store, cancelled=lambda: False)
        state = {"config": {"test_command": ["t"], "build_commands": [["b"]], "trusted_local": True, "command_timeout": 5,
                            "python_environment": str(self.python)}}
        with patch.object(quality, "run_check", side_effect=fake):
            quality.parallel_checks(flow, state, self.repo)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(env and env["PATH"].startswith(str(self.bin)) for env in seen))

    def test_current_registration_wins_including_a_cleared_declaration(self):
        from graph_engineering.accepted_development import current_environment
        frozen = {"python_environment": "/stale/venv/bin/python", "repo": "x"}
        self.assertNotIn("python_environment", current_environment(frozen, {}))  # Cleared since the ticket started.
        self.assertEqual(current_environment(frozen, {"python_environment": str(self.python)})["python_environment"], str(self.python))
        self.assertEqual(current_environment(frozen, None), frozen)  # Registration gone: keep the run's own value.


if __name__ == "__main__":
    unittest.main()
