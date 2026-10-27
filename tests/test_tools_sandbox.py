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
        work = self.root / "box" / "work"
        work.mkdir(parents=True)
        self.sandbox = Sandbox(self.root / "box", work, profiles=self.root / "profiles")

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

    def test_a_background_process_does_not_hold_the_command_open(self):
        """Run 9: `server &` kept the output pipe open, so every such command ran to the timeout."""
        code, out, seconds = self.sandbox.run("sleep 30 & echo started", timeout=20)
        self.assertEqual(code, 0)
        self.assertIn("started", out)
        self.assertIn("background processes this command started were stopped", out)
        self.assertLess(seconds, 10)
        code, out, _ = self.sandbox.run("echo plain")
        self.assertEqual(out.strip(), "plain")   # nothing left behind, nothing said

    def test_a_timeout_is_reported_as_a_probable_hang(self):
        result = Workspace(self.sandbox.work, self.sandbox, run_timeout=1).run("sleep 5")
        self.assertTrue(result.startswith("exit code 124"))
        self.assertIn("probably hangs", result)
        piped = Workspace(self.sandbox.work, self.sandbox, run_timeout=1).run("sleep 5 | tail -3")
        self.assertIn("prints nothing when the command is killed", piped)
        self.assertNotIn("prints nothing", result)

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
        sandbox = Sandbox(self.root / "box", self.sandbox.work, profiles=self.root / "profiles")
        _, out, _ = sandbox.run("env")
        self.assertNotIn("GE_SECRET_PROBE", out)
        self.assertNotIn("GITHUB_TOKEN", out)
        self.assertIn(f"HOME={self.root / 'box' / 'home'}", out)

    def test_javascript_syntax_is_checked_with_node(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("node missing")
        ws = Workspace(self.sandbox.work, self.sandbox)
        self.assertIn("breaks the syntax", ws.write_file("bad.js", "export function f( {\n"))
        self.assertTrue(ws.write_file("good.mjs", "export const f = () => 1;\n").startswith("Created"))

    def test_only_the_box_is_writable_and_git_metadata_is_read_only(self):
        root = self.root / "ticket"
        box, work = root / "box", root / "box" / "work"
        (work / ".git").mkdir(parents=True)
        (work / ".git" / "config").write_text("[core]\n")
        (root / "notes.md").write_text("private")
        sandbox = Sandbox(box, work, profiles=root / "profiles", private=[root])
        code, out, _ = sandbox.run("echo ok > file.txt && cat file.txt")
        self.assertEqual((code, out.strip()), (0, "ok"))
        for target in (root / "notes.md", root / "profiles" / "sandbox.sb", work / ".git" / "config", work / ".git" / "hooks" / "x"):
            code, _, _ = sandbox.run(f"echo x >> '{target}'")
            self.assertNotEqual(code, 0, target)
        self.assertEqual((work / ".git" / "config").read_text(), "[core]\n")
        code, _, _ = sandbox.run("mv .git moved")
        self.assertNotEqual(code, 0)
        with self.assertRaises(ValueError):
            Sandbox(box, work, profiles=box / "profiles")   # a profile the sandbox could rewrite

    def test_git_can_restore_a_file_but_not_commit_or_install_hooks(self):
        from graph_engineering.sandbox import prepare_workspace
        root = self.root / "ticket"
        work = root / "box" / "work"
        prepare_workspace(python_repo(self.root), "main", work)
        sandbox = Sandbox(root / "box", work, profiles=root / "profiles", private=[root])
        code, out, _ = sandbox.run("echo x >> calc.py && git checkout -- calc.py && git status --short")
        self.assertEqual((code, out.strip()), (0, ""))
        for command in ("echo y >> calc.py && git commit -qam y", "git stash", "echo h > .git/hooks/pre-commit",
                        "git config core.hooksPath hooks"):
            code, _, _ = sandbox.run(command)
            self.assertNotEqual(code, 0, command)

    def test_the_state_directory_is_unreadable_except_the_box(self):
        home = self.root / "state"
        (home / "ge.db").parent.mkdir(parents=True)
        (home / "ge.db").write_text("secret state")
        box = home / "work" / "GE-1" / "box"
        work = box / "work"
        work.mkdir(parents=True)
        (work / "readme.txt").write_text("visible")
        sandbox = Sandbox(box, work, profiles=home / "work" / "GE-1" / "profiles", private=[home])
        code, out, _ = sandbox.run(f"cat '{home / 'ge.db'}'")
        self.assertNotEqual(code, 0)
        self.assertNotIn("secret state", out)
        code, out, _ = sandbox.run("cat readme.txt")
        self.assertEqual((code, out.strip()), (0, "visible"))

    def test_a_closed_loopback_port_is_unreachable(self):
        import socket
        from graph_engineering import sandbox as module
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        probe = f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3); print('reached')\""
        code, out, _ = self.sandbox.run(probe)
        self.assertEqual(code, 0, out)
        module.CLOSED_PORTS.add(port)
        self.addCleanup(module.CLOSED_PORTS.discard, port)
        code, out, _ = self.sandbox.run(probe)
        self.assertNotEqual(code, 0)
        self.assertIn("not permitted", out)

    def test_timeout_kills_the_process_group(self):
        code, out, _ = self.sandbox.run("sleep 30 & sleep 30", timeout=1)
        self.assertEqual(code, 124)
        self.assertIn("killed after 1s", out)


if __name__ == "__main__":
    unittest.main()
