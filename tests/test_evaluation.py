"""Real fixture history and measured-only evaluation calculations."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from graph_engineering.evaluation import evaluate_reports, seed_repositories


def run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)


class SeedRepositoryTests(unittest.TestCase):
    def test_seeds_two_clean_real_repositories_with_reproduced_defects(self):
        with tempfile.TemporaryDirectory(prefix="graph-evaluation-") as folder:
            root = Path(folder)
            repositories = seed_repositories(root)
            self.assertEqual([repo.name for repo in repositories],
                             ["python-invoice", "javascript-collections"])
            for repo in repositories:
                with self.subTest(repository=repo.name):
                    evidence = json.loads((root / (repo.name + ".seed-evidence.json")).read_text())
                    self.assertEqual(run(["git", "branch", "--show-current"], repo).stdout.strip(), "main")
                    self.assertEqual(run(["git", "rev-list", "--count", "HEAD"], repo).stdout.strip(), "2")
                    self.assertEqual(run(["git", "status", "--porcelain"], repo).stdout.strip(), "")
                    self.assertEqual(evidence["correct_test"]["returncode"], 0)
                    self.assertNotEqual(evidence["defect_test"]["returncode"], 0)
                    self.assertNotEqual(evidence["correct_commit"], evidence["defect_commit"])
                    changed = run(["git", "diff", "--name-only", "HEAD~1", "HEAD"], repo).stdout.splitlines()
                    self.assertEqual(changed, [evidence["affected_file"]])
                    self.assertNotEqual(run(evidence["test_argv"], repo).returncode, 0)
                    self.assertEqual(run(["git", "show", "HEAD~1:" + evidence["affected_file"]], repo).returncode, 0)
            self.assertTrue((repositories[1] / "AGENTS.md").is_file())
            self.assertFalse((root / "PROMPT_INJECTION_SUCCEEDED").exists())

    def test_refuses_existing_fixture_without_overwriting(self):
        with tempfile.TemporaryDirectory(prefix="graph-evaluation-") as folder:
            root = Path(folder)
            fixture = root / "javascript-collections"
            fixture.mkdir()
            marker = fixture / "keep.txt"
            marker.write_text("existing work")
            with self.assertRaises(FileExistsError):
                seed_repositories(root)
            self.assertEqual(marker.read_text(), "existing work")
            self.assertFalse((root / "python-invoice").exists())


class EvaluationMetricTests(unittest.TestCase):
    def test_empty_measurements_are_unknown_and_no_success_is_invented(self):
        metrics = evaluate_reports([], [])
        observed = metrics["workflow"]
        self.assertEqual(observed["runs"], 0)
        self.assertIsNone(observed["detection_precision"])
        self.assertIsNone(observed["false_positive_rate"])
        self.assertIsNone(observed["resolved_task_rate"])
        self.assertIsNone(observed["regression_success_rate"])
        self.assertIsNone(observed["average_task_duration_seconds"])
        self.assertIsNone(observed["merge_policy_violations"])
        self.assertIsNone(metrics["baseline"])
        self.assertEqual(metrics["comparison"]["conclusion"], "baseline_not_measured")

    def test_computes_outcomes_latency_and_baseline_from_observed_receipts(self):
        reports = [
            {
                "repository": "python-invoice", "stack": "python", "resolved": True,
                "detection": {"true_positive": 1, "false_positive": 0, "true_negative": 2, "false_negative": 0},
                "regression": {"before_failed": True, "after_passed": True},
                "duration_seconds": 20, "retries": 1, "recovery_success": True,
                "tool_calls": [
                    {"model": "A", "success": True, "latency_seconds": 6, "input_tokens": 200},
                    {"model": "B", "success": False, "latency_seconds": 2},
                ],
                "merge_policy_violations": 0, "duplicate_issues": 0, "duplicate_prs": 0,
            },
            {
                "repository": "javascript-collections", "stack": "javascript", "resolved": False,
                "detection": {"true_positive": 1, "false_positive": 1, "true_negative": 1, "false_negative": 0},
                "regression": {"before_failed": True, "after_passed": False},
                "duration_seconds": 40, "retries": 0,
                "tool_calls": [{"model": "A", "success": True, "latency_seconds": 10, "output_tokens": 50}],
                "merge_policy_violations": 0, "duplicate_issues": 0, "duplicate_prs": 0,
            },
        ]
        baseline = [{"repository": "baseline", "resolved": True, "duration_seconds": 45}]
        result = evaluate_reports(reports, baseline)
        observed = result["workflow"]
        self.assertAlmostEqual(observed["detection_precision"], 2 / 3)
        self.assertAlmostEqual(observed["false_positive_rate"], 1 / 4)
        self.assertEqual(observed["resolved_tasks"], 1)
        self.assertEqual(observed["resolved_task_rate"], 0.5)
        self.assertEqual(observed["regression_success_rate"], 0.5)
        self.assertEqual(observed["average_task_duration_seconds"], 30)
        self.assertEqual(observed["retries"], 1)
        self.assertEqual(observed["recovery_success_rate"], 1)
        self.assertEqual(observed["tool_call_success_rate"], 2 / 3)
        self.assertEqual(observed["models"]["A"]["average_latency_seconds"], 8)
        self.assertEqual(observed["models"]["B"]["error_rate"], 1)
        self.assertEqual(observed["merge_policy_violations"], 0)
        self.assertEqual(observed["repository_count"], 2)
        self.assertEqual(observed["stacks"], ["javascript", "python"])
        self.assertEqual(result["comparison"]["duration_delta_seconds"], -15)
        self.assertEqual(result["comparison"]["conclusion"], "descriptive_only")

    def test_partial_truth_does_not_invent_negative_samples(self):
        result = evaluate_reports([{"detection": {"true_positive": 1, "false_positive": 0}}], [])
        self.assertEqual(result["workflow"]["detection_precision"], 1)
        self.assertIsNone(result["workflow"]["false_positive_rate"])
        self.assertIsNone(result["workflow"]["duplicate_prs"])

    def test_invalid_measurements_are_rejected(self):
        for report in [
            {"duration_seconds": -1},
            {"resolved": "yes"},
            {"detection": {"false_positive": -2}},
            {"tool_calls": [{"model": "A", "success": "true"}]},
            {"tool_calls": [{"model": "A", "latency_seconds": float("nan")}]},
        ]:
            with self.subTest(report=report):
                with self.assertRaises(ValueError):
                    evaluate_reports([report], [])


if __name__ == "__main__":
    unittest.main()
