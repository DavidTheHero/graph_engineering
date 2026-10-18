"""Policy tests and real local Git publication round trips."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graph_engineering.publication import (
    APIError, GitHubBackend, LocalBackend, PublicationError, merge_gate,
)


class MergeGateTests(unittest.TestCase):
    def snapshot(self, **changes):
        value = dict(head="a" * 40, base_head="b" * 40,
                     checks_passed=True, checks_present=True,
                     reviews_satisfied=True, mergeable=True, draft=False,
                     policy_known=True, merged=False, check_names=["unit"])
        value.update(changes)
        return value

    def test_missing_approval_blocks_and_approval_binds_exact_head(self):
        head = "a" * 40
        self.assertIn("human approval missing or stale", merge_gate(self.snapshot(), None, head, True))
        self.assertEqual([], merge_gate(self.snapshot(), head, head, True))
        self.assertIn("independent tests missing or stale", merge_gate(self.snapshot(), head, "c" * 40, True))

    def test_every_failure_blocks_and_auto_only_allows_low_risk(self):
        head = "a" * 40
        for field in ("checks_passed", "checks_present", "reviews_satisfied", "mergeable", "policy_known"):
            with self.subTest(field=field):
                self.assertTrue(merge_gate(self.snapshot(**{field: False}), head, head, True))
        self.assertTrue(merge_gate(self.snapshot(draft=True), head, head, True))
        self.assertTrue(merge_gate(self.snapshot(), head, head, False))
        self.assertEqual([], merge_gate(self.snapshot(), None, head, True, trusted_auto=True))
        self.assertTrue(merge_gate(self.snapshot(), None, head, True, trusted_auto=True, risk="high"))
        self.assertTrue(merge_gate(self.snapshot(), head, head, True, required_checks=["missing"]))

    def test_missing_fields_fail_closed(self):
        self.assertTrue(merge_gate({}, None, None, False))


class LocalBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "file.txt").write_text("before\n")
        self.git("add", "file.txt")
        self.git("commit", "-m", "initial")
        self.git("checkout", "-b", "graph/task")
        (self.repo / "file.txt").write_text("after\n")
        self.git("commit", "-am", "fix")
        self.head = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        self.backend = LocalBackend(self.root / "state", self.repo, "main")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT, text=True).strip()

    def test_issue_and_pr_retry_is_idempotent_and_checks_bind_head(self):
        issue = self.backend.ensure_issue("task", "Fix", "Evidence", ["bug"])
        self.assertEqual(issue, self.backend.ensure_issue("task", "Fix", "Evidence", ["bug"]))
        pr = self.backend.ensure_pr("task", "Fix", "Fixes #1", "graph/task", self.head)
        self.assertEqual(pr, self.backend.ensure_pr("task", "Fix", "Fixes #1", "graph/task", self.head))
        self.assertFalse(self.backend.inspect_pr(pr["number"])["checks_passed"])
        self.backend.record_check(pr["number"], self.head, True)
        self.assertTrue(self.backend.inspect_pr(pr["number"])["checks_passed"])
        self.assertEqual(1, len(json.loads(self.backend.path.read_text())["issues"]))
        with self.assertRaises(PublicationError):
            self.backend.record_check(pr["number"], "c" * 40, True)

    def test_merge_is_real_and_resumes_without_another_commit(self):
        pr = self.backend.ensure_pr("task", "Fix", "Evidence", "graph/task", self.head)
        with self.assertRaises(PublicationError):
            self.backend.merge(pr["number"], self.head)
        self.backend.record_check(pr["number"], self.head, True)
        result = self.backend.merge(pr["number"], self.head)
        self.assertTrue(result["merged"])
        self.assertEqual("after\n", (self.repo / "file.txt").read_text())
        self.assertEqual(result["merge_commit"], self.git("rev-parse", "main"))
        self.assertEqual(result, self.backend.merge(pr["number"], self.head))

    def test_dirty_repo_and_changed_pr_head_block_merge(self):
        pr = self.backend.ensure_pr("task", "Fix", "Evidence", "graph/task", self.head)
        self.backend.record_check(pr["number"], self.head, True)
        (self.repo / "untracked").write_text("keep me")
        with self.assertRaises(PublicationError):
            self.backend.merge(pr["number"], self.head)
        (self.repo / "untracked").unlink()
        with self.assertRaises(PublicationError):
            self.backend.merge(pr["number"], "d" * 40)

    def test_reconciles_merge_that_finished_before_checkpoint(self):
        pr = self.backend.ensure_pr("task", "Fix", "Evidence", "graph/task", self.head)
        self.backend.record_check(pr["number"], self.head, True)
        original_save = self.backend._save
        calls = []

        def fail_after_merge(state):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("injected controller crash")
            original_save(state)

        with patch.object(self.backend, "_save", side_effect=fail_after_merge):
            with self.assertRaises(OSError):
                self.backend.merge(pr["number"], self.head)
        recovered = LocalBackend(self.root / "state", self.repo, "main").merge(pr["number"], self.head)
        self.assertTrue(recovered["merged"])
        self.assertEqual(recovered["merge_commit"], self.git("rev-parse", "main"))


class GitHubBackendTests(unittest.TestCase):
    def backend(self):
        return GitHubBackend(Path("."), Path("."), "owner/repo", "main")

    def test_duplicate_markers_block_creation(self):
        backend = self.backend()
        with patch.object(backend, "_list", return_value=[
            dict(number=1, body="<!-- graph-engineering:task -->"),
            dict(number=2, body="<!-- graph-engineering:task -->"),
        ]):
            with self.assertRaises(PublicationError):
                backend.ensure_issue("task", "title", "body")

    def test_uncertain_issue_creation_reconciles_observed_marker(self):
        backend = self.backend()
        with patch.object(backend, "_list", side_effect=[[], [], [dict(number=3, html_url="url", body="<!-- graph-engineering:task -->")]]), \
             patch.object(backend, "_api", side_effect=PublicationError("timeout")):
            result = backend.ensure_issue("task", "title", "body")
        self.assertEqual(3, result["number"])

    def test_policy_read_error_fails_closed(self):
        backend = self.backend()
        with patch.object(backend, "_api", side_effect=PublicationError("unreadable")):
            with self.assertRaises(PublicationError):
                backend.inspect_pr(1)

    def inspect_fixture(self, *, rules=None, protection=None, runs=None,
                        reviews=None, review_decision=None, graph_head=None):
        head, base = "a" * 40, "b" * 40
        backend = self.backend()

        def api(endpoint, method="GET", payload=None):
            if endpoint == "repos/owner/repo/pulls/1":
                return dict(head=dict(sha=head), base=dict(ref="main"), user=dict(login="author"),
                            mergeable=True, mergeable_state="clean", state="open", draft=False,
                            merged=False, merge_commit_sha=None)
            if endpoint == "repos/owner/repo/branches/main":
                return dict(protected=bool(protection or rules), commit=dict(sha=base))
            if endpoint.startswith("repos/owner/repo/rules/branches/main?"):
                return rules or []
            if endpoint.endswith("/protection"):
                if protection is None:
                    raise APIError("not protected", 404)
                return protection
            if "/check-runs?" in endpoint:
                return dict(check_runs=runs if runs is not None else [
                    dict(name="unit", status="completed", conclusion="success", app=dict(id=7))])
            if "/status?" in endpoint:
                return dict(total_count=0, statuses=[])
            if "/reviews?" in endpoint:
                return reviews or []
            if endpoint == "graphql":
                return dict(data=dict(repository=dict(pullRequest=dict(
                    headRefOid=graph_head or head, baseRefOid=base,
                    reviewDecision=review_decision, mergeStateStatus="CLEAN"))))
            raise AssertionError(f"unexpected endpoint {endpoint}")

        with patch.object(backend, "_api", side_effect=api):
            return backend.inspect_pr(1)

    def test_observed_unprotected_policy_and_passing_checks_are_known(self):
        value = self.inspect_fixture()
        self.assertTrue(value["policy_known"])
        self.assertTrue(value["checks_passed"])
        self.assertTrue(value["reviews_satisfied"])

    def test_required_review_requires_independent_current_head_approval(self):
        policy = dict(required_pull_request_reviews=dict(required_approving_review_count=1))
        author_approval = dict(state="APPROVED", commit_id="a" * 40, user=dict(login="author"))
        stale_approval = dict(state="APPROVED", commit_id="c" * 40, user=dict(login="reviewer"))
        valid_approval = dict(state="APPROVED", commit_id="a" * 40, user=dict(login="reviewer"))
        for approvals in ([], [author_approval], [stale_approval]):
            with self.subTest(approvals=approvals):
                self.assertFalse(self.inspect_fixture(protection=policy, reviews=approvals,
                                                      review_decision="APPROVED")["reviews_satisfied"])
        self.assertTrue(self.inspect_fixture(protection=policy, reviews=[valid_approval],
                                             review_decision="APPROVED")["reviews_satisfied"])
        self.assertFalse(self.inspect_fixture(protection=policy, reviews=[valid_approval],
                                              review_decision="REVIEW_REQUIRED")["reviews_satisfied"])

    def test_required_check_source_and_unknown_rules_block(self):
        rules = [dict(type="required_status_checks", parameters=dict(
            required_status_checks=[dict(context="unit", integration_id=8)]))]
        self.assertFalse(self.inspect_fixture(rules=rules)["checks_passed"])
        rules[0]["parameters"]["required_status_checks"][0]["integration_id"] = 7
        self.assertTrue(self.inspect_fixture(rules=rules)["checks_passed"])
        self.assertFalse(self.inspect_fixture(rules=[dict(type="merge_queue")])["policy_known"])
        self.assertFalse(self.inspect_fixture(graph_head="d" * 40)["policy_known"])
        self.assertFalse(self.inspect_fixture(runs=[])["checks_passed"])

    def test_merge_uses_exact_head_and_requires_server_observation(self):
        backend = self.backend()
        head = "a" * 40
        snapshot = MergeGateTests().snapshot()
        with patch.object(backend, "inspect_pr", return_value=snapshot), \
             patch.object(backend, "_api", side_effect=[dict(sha="f" * 40),
                          dict(merged=True, head=dict(sha=head), merge_commit_sha="f" * 40)]) as api:
            result = backend.merge(1, head)
        self.assertEqual("f" * 40, result["merge_commit"])
        self.assertEqual(dict(sha=head, merge_method="merge"), api.call_args_list[0].args[2])
        with patch.object(backend, "inspect_pr", return_value=snapshot), \
             patch.object(backend, "_api", side_effect=[dict(sha="f" * 40), dict(merged=False)]):
            with self.assertRaises(PublicationError):
                backend.merge(1, head)

    def test_changed_head_never_reaches_merge_endpoint(self):
        backend = self.backend()
        with patch.object(backend, "inspect_pr", return_value=MergeGateTests().snapshot()), \
             patch.object(backend, "_api") as api:
            with self.assertRaises(PublicationError):
                backend.merge(1, "d" * 40)
        api.assert_not_called()

    def test_record_check_publishes_actual_result_for_exact_head(self):
        backend = self.backend()
        head = "a" * 40
        with patch.object(backend, "_api", side_effect=[dict(head=dict(sha=head)), [] , dict(state="success")]) as api:
            backend.record_check(1, head, True)
        self.assertEqual("repos/owner/repo/statuses/" + head, api.call_args_list[-1].args[0])
        self.assertEqual("success", api.call_args_list[-1].args[2]["state"])
        with patch.object(backend, "_api", return_value=dict(head=dict(sha="c" * 40))):
            with self.assertRaises(PublicationError):
                backend.record_check(1, head, True)

    def test_publish_review_posts_comment_for_actual_head(self):
        backend = self.backend()
        with patch.object(backend, "_api", side_effect=[[], dict(head=dict(sha="a" * 40)),
                          dict(id=42, state="COMMENTED")]) as api:
            result = backend.publish_review(1, "Observed diff review")
        self.assertEqual(42, result["id"])
        self.assertEqual(dict(body="Observed diff review", event="COMMENT", commit_id="a" * 40),
                         api.call_args_list[-1].args[2])


if __name__ == "__main__":
    unittest.main()
