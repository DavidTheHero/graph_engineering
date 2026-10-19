import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from graph_engineering import cli
from graph_engineering.runtime import Store


class CLITests(unittest.TestCase):
    def test_ui_uses_selected_controller_without_creating_execution_state(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "uninitialised"
            with patch("graph_engineering.dashboard.serve_dashboard") as dashboard:
                code, _, error = self.invoke("--home", str(home), "ui", "--port", "8766")
            self.assertEqual(code, 0, error)
            dashboard.assert_called_once_with(home.resolve(), 8766)
            self.assertFalse(home.exists())

    def test_ui_rejects_invalid_port(self):
        with patch("graph_engineering.dashboard.serve_dashboard") as dashboard:
            code, _, error = self.invoke("ui", "--port", "65536")
        self.assertEqual(code, 1)
        self.assertIn("port must be", error)
        dashboard.assert_not_called()

    def test_enqueue_persists_ready_run_without_executing_it(self):
        from tests.test_core import make_repo
        from graph_engineering.workflow import initialise
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            config = initialise(repo, root / "controller", trusted_local=True)
            code, output, error = self.invoke("--home", str(root / "controller"), "enqueue", "--config", str(config))
            self.assertEqual(code, 0, error)
            result = json.loads(output)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["node"], "repository_discovery")
            self.assertNotIn("repository", result["state"])

    def invoke(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cli.main(list(argv))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_status_stop_and_exact_head_approval_use_persistent_store(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            store.create_run({"repo": str(root / "target")}, run_id="run1")
            store.checkpoint("run1", "ci", {"head": "a" * 40}, "approval_gate", "waiting_approval")
            store.close()
            code, output, error = self.invoke("--home", folder, "status", "run1", "--events")
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["run"]["status"], "waiting_approval")
            self.assertEqual(len(json.loads(output)["events"]), 2)
            code, output, error = self.invoke("--home", folder, "approve", "run1", "--head", "b" * 40, "--actor", "human")
            self.assertEqual(code, 1)
            self.assertIn("exact tested head", error)
            code, output, error = self.invoke("--home", folder, "approve", "run1", "--head", "a" * 40, "--actor", "human")
            self.assertEqual(code, 0, error)
            code, output, error = self.invoke("--home", folder, "stop", "run1")
            self.assertEqual(code, 0, error)
            store = Store(root)
            self.assertEqual(store.approved_head("run1"), "a" * 40)
            self.assertEqual(store.get("run1")["cancel"], 1)
            store.close()

    def test_execute_reports_blocked_with_exit_two(self):
        workflow = Mock()
        workflow.new_run.return_value = {"id": "blocked1"}
        workflow.execute.return_value = {"id": "blocked1", "node": "model_selection", "status": "blocked", "state": {}}
        with patch.object(cli, "_workflow", return_value=workflow):
            code, output, error = self.invoke("execute", "--config", "task.json", "--until", "test_execution")
        self.assertEqual(code, 2, error)
        self.assertEqual(json.loads(output)["status"], "blocked")
        workflow.execute.assert_called_once_with("blocked1", until="test_execution")
        workflow.store.close.assert_called_once()

    def test_resume_clears_stop_before_execution(self):
        workflow = Mock()
        workflow.execute.return_value = {"id": "run1", "node": "approval_gate", "status": "waiting_approval", "state": {}}
        with patch.object(cli, "_workflow", return_value=workflow):
            code, output, error = self.invoke("resume", "run1")
        self.assertEqual(code, 0, error)
        workflow.store.clear_stop.assert_called_once_with("run1")

    def test_serve_only_dispatches_ready_or_exact_head_approved_runs(self):
        workflow = Mock()
        workflow.store.list_runs.return_value = [
            {"id": "ready", "status": "ready", "state": {}},
            {"id": "waiting", "status": "waiting_approval", "state": {"head": "a" * 40}},
            {"id": "approved", "status": "waiting_approval", "state": {"head": "b" * 40}},
            {"id": "blocked", "status": "blocked", "state": {}},
        ]
        workflow.store.approved_head.side_effect = lambda run: "b" * 40 if run == "approved" else None
        workflow.execute.side_effect = lambda run: {"id": run, "node": "completion", "status": "complete", "state": {}}
        with patch.object(cli, "_workflow", return_value=workflow):
            code, output, error = self.invoke("serve", "--once")
        self.assertEqual(code, 0, error)
        self.assertEqual([call.args[0] for call in workflow.execute.call_args_list], ["ready", "approved"])
        self.assertEqual(len(output.splitlines()), 2)

    def test_evaluate_emits_unknown_values_and_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            reports = Path(folder) / "reports.json"
            reports.write_text("[]", encoding="utf-8")
            code, output, error = self.invoke("evaluate", "--reports", str(reports))
            self.assertEqual(code, 0, error)
            self.assertIsNone(json.loads(output)["workflow"]["resolved_task_rate"])
        with patch.dict("os.environ", {"TEST_API_KEY": "sensitive-example-value"}):
            with patch.object(cli, "_workflow", side_effect=RuntimeError("sensitive-example-value")):
                code, output, error = self.invoke("resume", "run1")
        self.assertEqual(code, 1)
        self.assertNotIn("sensitive-example-value", error)
        self.assertIn("REDACTED", error)

    def test_invalid_test_argv_and_poll_are_rejected_by_parser(self):
        for argv in [("init", "repo", "--test-command", '"python -m unittest"'),
                     ("serve", "--poll", "31")]:
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.parser().parse_args(argv)
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
