"""Windows background launches must never allocate a user-facing console."""
import os
import json
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock,patch

from graph_engineering import models,repository,publication


@unittest.skipUnless(os.name=='nt','Windows console process contract')
class WindowlessProcessTests(unittest.TestCase):
    def assert_hidden(self,call):
        startup=call.kwargs.get('startupinfo')
        self.assertIsNotNone(startup)
        self.assertTrue(startup.dwFlags&subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startup.wShowWindow,subprocess.SW_HIDE)

    def test_repository_command_is_hidden(self):
        child=MagicMock();child.poll.return_value=0;child.returncode=0
        with tempfile.TemporaryDirectory() as tmp,patch('graph_engineering.repository.subprocess.Popen',return_value=child) as launch:
            repository.command(['node','--version'],Path(tmp));self.assert_hidden(launch.call_args)

    def test_repository_git_is_hidden(self):
        with tempfile.TemporaryDirectory() as tmp,patch('graph_engineering.repository.subprocess.run') as launch:
            launch.return_value.returncode=0;launch.return_value.stdout='head'
            repository.git(Path(tmp),'rev-parse','HEAD');self.assert_hidden(launch.call_args)

    def test_model_runner_and_version_probe_are_hidden(self):
        child=MagicMock();child.communicate.return_value=('output','');child.returncode=0
        with tempfile.TemporaryDirectory() as tmp,patch('graph_engineering.models.subprocess.Popen',return_value=child) as launch:
            models._run_opencode(['opencode','run'],Path(tmp),{},10);self.assert_hidden(launch.call_args)
        with patch('graph_engineering.models.shutil.which',return_value='opencode'),patch('graph_engineering.models.subprocess.run') as version:
            version.return_value.returncode=0;version.return_value.stdout='opencode v2.0.32'
            models.detect_opencode();self.assert_hidden(version.call_args)

    def test_privileged_git_and_github_cli_are_hidden(self):
        with tempfile.TemporaryDirectory() as tmp,patch('graph_engineering.publication.subprocess.run') as launch:
            launch.return_value.returncode=0;launch.return_value.stdout='{}'
            publication._git(Path(tmp),'rev-parse','HEAD');self.assert_hidden(launch.call_args)
            backend=publication.GitHubBackend(Path(tmp),Path(tmp),'example/fixture','main')
            backend._api('repos/example/fixture');self.assert_hidden(launch.call_args)

    def test_cancellation_helper_is_hidden(self):
        child=MagicMock();child.poll.return_value=None;child.pid=123
        with patch('graph_engineering.repository.subprocess.run') as launch:
            repository.kill_tree(child);self.assert_hidden(launch.call_args)

    def test_real_command_and_its_child_inherit_an_invisible_console(self):
        probe='import ctypes,json; k=ctypes.WinDLL("kernel32"); k.GetConsoleWindow.restype=ctypes.c_void_p; h=k.GetConsoleWindow(); u=ctypes.WinDLL("user32"); u.IsWindowVisible.argtypes=[ctypes.c_void_p]; print(json.dumps({"console":h,"visible":bool(u.IsWindowVisible(h))}))'
        parent='import subprocess,sys; exec('+repr(probe)+'); subprocess.run([sys.executable,"-c",'+repr(probe)+'],check=True)'
        with tempfile.TemporaryDirectory() as tmp:
            result=repository.command([sys.executable,'-c',parent],Path(tmp),timeout=10)
        self.assertEqual(result['returncode'],0,result['output'])
        receipts=[json.loads(line) for line in result['output'].splitlines()]
        self.assertEqual(len(receipts),2)
        self.assertTrue(all(not row['visible'] for row in receipts),receipts)
        self.assertEqual(receipts[0]['console'],receipts[1]['console'])
