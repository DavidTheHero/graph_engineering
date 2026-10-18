import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graph_engineering.repository import Worktree, source_bundle
from graph_engineering.runtime import Store, redact
from test_core import git, make_repo


class HardeningTests(unittest.TestCase):
    def test_store_rejects_escaping_run_identifiers(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "state")
            try:
                for run_id in ("../escape", "../../escape", "a/b", "C:\\escape", "", "x" * 65):
                    with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                        store.create_run({"repo": td}, run_id=run_id)
                    with self.subTest(artifact=run_id), self.assertRaises(ValueError):
                        store.artifact(run_id, "result.json", {"safe": False})
            finally:
                store.close()

    def test_redacts_secrets_with_json_escapes(self):
        secret = 'long\\secret\nwith"quotes'
        with patch.dict(os.environ, {"DEMO_API_KEY": secret}):
            self.assertEqual({"value": "key=[REDACTED]"}, redact({"value": "key=" + secret}))

    def test_existing_worktree_from_other_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            original = git(repo, "rev-parse", "HEAD")
            other_root = root / "other"
            other_root.mkdir()
            other = make_repo(other_root)
            git(other, "checkout", "-b", "graph/task")
            with self.assertRaises(ValueError):
                Worktree(repo, other, "graph/task", original).ensure()

    def test_existing_worktree_must_descend_from_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            original = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "--orphan", "graph/unrelated")
            git(repo, "commit", "-m", "unrelated")
            git(repo, "checkout", "main")
            git(repo, "worktree", "add", str(root / "wt"), "graph/unrelated")
            with self.assertRaises(ValueError):
                Worktree(repo, root / "wt", "graph/unrelated", original).ensure()
            git(repo, "worktree", "remove", str(root / "wt"))

    def test_source_bundle_skips_oversized_files_without_full_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "huge.py").write_bytes(b"x" * 1_100_000)
            (root / "small.py").write_text("print('small')\n")
            with patch.object(Path, "read_text", side_effect=AssertionError("unbounded read")):
                bundle = source_bundle(root, ["huge.py", "small.py"], "", limit=1000)
            self.assertEqual(["small.py"], [item["path"] for item in bundle["files"]])
            self.assertLessEqual(bundle["used_characters"], 1000)


if __name__ == "__main__":
    unittest.main()
