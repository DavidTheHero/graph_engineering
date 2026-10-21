import json
from pathlib import Path
import shutil
import subprocess
import unittest

from graph_engineering import workflow
from graph_engineering.publish import Local, merge_gate, pending_only
from graph_engineering.repos import Repo

from helpers import TEST_COMMAND, git, python_repo, temp_dir

GOOD = {"head": "a" * 40, "checks_present": True, "checks_passed": True, "reviews_satisfied": True, "mergeable": True,
        "policy_known": True, "draft": False, "check_names": ["Ticket and scope", "tests"]}
POLICY = Path("/Users/daviddoan/git_code/General_AI_Agent/scripts/pr-policy.mjs")


class MergeGate(unittest.TestCase):
    def test_everything_at_one_head(self):
        self.assertEqual(merge_gate(GOOD, "a" * 40, ["Ticket and scope"]), [])

    def test_each_missing_condition_blocks(self):
        self.assertIn("approval missing or given for another commit", merge_gate(GOOD, "b" * 40))
        for key in ("checks_passed", "reviews_satisfied", "mergeable", "policy_known"):
            self.assertTrue(merge_gate({**GOOD, key: False}, "a" * 40), key)
            self.assertTrue(merge_gate({k: v for k, v in GOOD.items() if k != key}, "a" * 40), f"{key} missing")
        self.assertIn("PR is a draft", merge_gate({**GOOD, "draft": True}, "a" * 40))
        self.assertTrue(merge_gate(GOOD, "a" * 40, ["Platform (Windows)"]))

    def test_waiting_is_distinguished_from_refusal(self):
        self.assertTrue(pending_only(merge_gate({**GOOD, "checks_passed": False}, "a" * 40)))
        self.assertFalse(pending_only(merge_gate({**GOOD, "checks_passed": False, "policy_known": False}, "a" * 40)))
        self.assertFalse(pending_only(merge_gate(GOOD, None)))


class PullRequestConvention(unittest.TestCase):
    TICKET = {"id": "GE-0A1B2C3D", "spec": {"title": "Display and select the current AI model!", "kind": "feat",
                                            "acceptance_criteria": ["GET /api/model returns the model"]},
              "data": {"head": "a" * 40, "base": "b" * 40, "summary": "Added the endpoint and selector.",
                       "gates": [{"name": "test suite passes", "ok": True}], "review": {"model": "qwen27b", "verdict": "approve", "summary": "Good."},
                       "rounds": [{}]}}

    def test_branch_follows_the_repository_template(self):
        repo = Repo(name="r", path="/tmp", test_command="t")
        self.assertEqual(workflow.branch_name(repo, self.TICKET, 21), "feat/21-display-and-select-the-current-ai-model")
        self.assertEqual(workflow.branch_name(repo, self.TICKET, None), "ge/ge-0a1b2c3d-display-and-select-the-current-ai-model")

    @unittest.skipUnless(POLICY.exists() and shutil.which("node"), "General_AI_Agent checkout or node missing")
    def test_general_ai_agent_policy_accepts_the_pr(self):
        repo = Repo(name="r", path="/tmp", test_command="t")
        branch = workflow.branch_name(repo, self.TICKET, 21)
        body = workflow.pr_body(self.TICKET, 21)
        script = (f"import {{ validatePullRequest }} from {json.dumps(POLICY.as_uri())};"
                  f"console.log(JSON.stringify(validatePullRequest({json.dumps({'branch': branch, 'body': body, 'files': [{'filename': 'backend/app.js', 'status': 'modified'}]})})));")
        result = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(result, {"issueNumber": 21, "errors": []})


class LocalPublication(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.repo = python_repo(self.root)
        self.work = self.root / "work"
        git(self.root, "clone", "-q", str(self.repo), str(self.work))
        git(self.work, "config", "user.email", "t@example.com")
        git(self.work, "config", "user.name", "t")
        (self.work / "calc.py").write_text("def add(a, b):\n    return a + b  # changed\n")
        git(self.work, "commit", "-qam", "change")
        self.head = git(self.work, "rev-parse", "HEAD").strip()
        self.local = Local(self.repo, "main")

    def test_publish_then_fast_forward_merge(self):
        pr = self.local.ensure_pr("GE-1", self.work, "x", self.head, "t", "b")
        self.assertEqual(pr["branch"], "ge/GE-1")
        self.assertEqual(self.local.merge("ge/GE-1", "f" * 40)["merged"], False)
        self.assertTrue(self.local.merge("ge/GE-1", self.head)["merged"])
        self.assertIn("# changed", (self.repo / "calc.py").read_text())

    def test_uncommitted_work_on_base_is_never_disturbed(self):
        self.local.ensure_pr("GE-1", self.work, "x", self.head, "t", "b")
        (self.repo / "calc.py").write_text("my local edit\n")
        result = self.local.merge("ge/GE-1", self.head)
        self.assertFalse(result["merged"])
        self.assertEqual((self.repo / "calc.py").read_text(), "my local edit\n")

    def test_moved_base_needs_another_round(self):
        self.local.ensure_pr("GE-1", self.work, "x", self.head, "t", "b")
        (self.repo / "other.py").write_text("x = 1\n")
        git(self.repo, "add", "other.py")
        git(self.repo, "commit", "-qm", "someone else")
        result = self.local.merge("ge/GE-1", self.head)
        self.assertFalse(result["merged"])
        self.assertIn("moved on", result["reasons"][0])


if __name__ == "__main__":
    unittest.main()
