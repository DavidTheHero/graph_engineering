import os
from pathlib import Path
import unittest

from graph_engineering.sandbox import Sandbox, available
from graph_engineering.tools import READ_ONLY, Workspace

from helpers import python_repo, temp_dir


class Tools(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.ws = Workspace(python_repo(self.root))

    def test_paths_cannot_leave_the_workspace(self):
        for path in ("../outside.txt", "/etc/passwd", ".git/config", "tests/../../x"):
            self.assertTrue(self.ws.call("read_file", {"path": path}, READ_ONLY).startswith("Error"), path)

    def test_read_only_session_cannot_write(self):
        self.assertIn("no tool named write_file", self.ws.call("write_file", {"path": "a", "content": "b"}, READ_ONLY))

    def test_edit_errors_explain_how_to_recover(self):
        (self.ws.root / "dup.py").write_text("x = 1\nx = 1\n    y = 2\n")
        self.assertIn("occurs 2 times", self.ws.edit_file("dup.py", "x = 1", "x = 2"))
        self.assertIn("check indentation", self.ws.edit_file("dup.py", "y = 2\n\n", "y = 3"))
        self.assertIn("Read the file again", self.ws.edit_file("dup.py", "z = 9", "z = 1"))
        (self.ws.root / "near.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
        error = self.ws.edit_file("near.py", "def sub(x, y):\n    return x - y", "pass")
        self.assertIn("    5| def sub(a, b):\n    6|     return a - b", error)  # the retry needs no re-read

    def test_read_pages_and_caps_output(self):
        (self.ws.root / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 1001)))
        page = self.ws.read_file("long.txt", start_line=401)
        self.assertIn("  401| line 401", page)
        self.assertIn("[lines 801-1000 not shown]", page)
        (self.ws.root / "wide.txt").write_text("\n".join("w" * 1500 for _ in range(100)))
        self.assertLess(len(self.ws.read_file("wide.txt")), 26000)

    def test_an_edit_that_breaks_syntax_is_not_applied(self):
        before = (self.ws.root / "calc.py").read_text()
        reply = self.ws.edit_file("calc.py", "return a + b", "return a +")
        self.assertIn("not applied because it breaks the syntax of calc.py", reply)
        self.assertEqual((self.ws.root / "calc.py").read_text(), before)
        reply = self.ws.write_file("new.json", "{bad json")
        self.assertTrue(reply.startswith("Error"))
        self.assertFalse((self.ws.root / "new.json").exists())
        self.assertTrue(self.ws.write_file("ok.py", "x = 1\n").startswith("Created"))

    def test_search_and_list_include_new_files(self):
        self.ws.write_file("new_module.py", "def subtract():\n    pass\n")
        self.assertIn("new_module.py:1:def subtract", self.ws.search("subtract"))
        self.assertIn("new_module.py", self.ws.list_files())
        self.assertEqual(self.ws.search("nothing_matches_this"), "No matches")


@unittest.skipUnless(available(), "no OS sandbox on this host")
class Confinement(unittest.TestCase):
    """The sandbox is the trust boundary, so these run the real mechanism, not a mock."""

    def setUp(self):
        self.root = temp_dir(self)
        work = self.root / "work"
        work.mkdir()
        self.sandbox = Sandbox(self.root, work)

    def test_writes_are_confined_to_the_run_directory(self):
        outside = Path.home() / f".ge-sandbox-probe-{os.getpid()}"
        code, out, _ = self.sandbox.run(f"echo inside > ok.txt && cat ok.txt && echo x > {outside}")
        self.assertIn("inside", out)
        self.assertNotEqual(code, 0)
        self.assertFalse(outside.exists())

    def test_a_pipe_keeps_the_failing_exit_code(self):
        """Live: the coder ran `npm test 2>&1 | tail -25` and read tail's exit code 0 as a passing suite."""
        code, _, _ = self.sandbox.run("exit 3 | tail -1")
        self.assertNotEqual(code, 0)

    def test_a_timeout_is_reported_as_a_probable_hang(self):
        result = Workspace(self.sandbox.work, self.sandbox, run_timeout=1).run("sleep 5")
        self.assertTrue(result.startswith("exit code 124"))
        self.assertIn("probably hangs", result)

    def test_the_agent_command_limit_follows_the_base_suite(self):
        from graph_engineering.repos import Repo
        from graph_engineering.workflow import run_timeout
        repo = Repo(name="r", path="/x", test_command="true")
        self.assertEqual(run_timeout(repo, 10), 70)
        self.assertEqual(run_timeout(repo, 1), 60)
        self.assertEqual(run_timeout(repo, 200), repo.run_timeout)
        self.assertEqual(run_timeout(repo, None), repo.run_timeout)  # the base suite failed: keep the limit

    def test_credentials_are_unreadable(self):
        ssh = Path.home() / ".ssh"
        if not ssh.exists():
            self.skipTest("no ~/.ssh on this host")
        code, out, _ = self.sandbox.run(f"ls {ssh}")
        self.assertNotEqual(code, 0)

    def test_network_is_closed_except_localhost(self):
        code, _, _ = self.sandbox.run("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)\"")
        self.assertNotEqual(code, 0)
        code, out, _ = self.sandbox.run("python3 -c \"import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); "
                                        "socket.create_connection(s.getsockname()); print('local ok')\"")
        self.assertEqual(code, 0, out)

    def test_environment_is_minimal(self):
        os.environ["GE_SECRET_PROBE"] = "leak"
        self.addCleanup(os.environ.pop, "GE_SECRET_PROBE")
        sandbox = Sandbox(self.root, self.root / "work")
        _, out, _ = sandbox.run("env")
        self.assertNotIn("GE_SECRET_PROBE", out)
        self.assertNotIn("GITHUB_TOKEN", out)
        self.assertIn(f"HOME={self.root / 'home'}", out)

    def test_javascript_syntax_is_checked_with_node(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("node missing")
        ws = Workspace(self.root / "work", self.sandbox)
        self.assertIn("breaks the syntax", ws.write_file("bad.js", "export function f( {\n"))
        self.assertTrue(ws.write_file("good.mjs", "export const f = () => 1;\n").startswith("Created"))

    def test_timeout_kills_the_process_group(self):
        code, out, _ = self.sandbox.run("sleep 30 & sleep 30", timeout=1)
        self.assertEqual(code, 124)
        self.assertIn("killed after 1s", out)


if __name__ == "__main__":
    unittest.main()
