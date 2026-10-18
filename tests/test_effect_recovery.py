import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from graph_engineering.runtime import Store
from graph_engineering.publication import GitHubBackend, PublicationError


class RecoveryTests(unittest.TestCase):
    def test_existing_unmarked_issue_blocks_speculative_duplicate(self):
        backend = GitHubBackend(Path("."), Path("."), "owner/repo", "main")
        existing = {"number": 8, "state": "open", "title": "Add returns wrong value", "body": "Previously reported"}
        with patch.object(backend, "_list", return_value=[existing]), patch.object(backend, "_api") as api:
            with self.assertRaises(PublicationError):
                backend.ensure_issue("new-key", "Add returns wrong value", "Reproduced assertion")
            api.assert_not_called()

    def test_uncertain_write_never_blindly_replays_creation(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            store.create_run({"repo": td}, "run")
            with self.assertRaises(ConnectionError):
                store.effect("run", "issue", lambda: (_ for _ in ()).throw(ConnectionError("lost reply")))
            called = []
            with self.assertRaises(RuntimeError):
                store.effect("run", "issue", lambda: called.append(1) or {"number": 2})
            self.assertEqual(called, [])
            result = store.effect("run", "issue", lambda: self.fail("mutation"),
                                  on_uncertain=lambda: {"number": 1})
            self.assertEqual(result["number"], 1)
            store.close()

    def test_reconciliation_missing_marker_never_posts(self):
        backend = GitHubBackend(Path("."), Path("."), "owner/repo", "main")
        with patch.object(backend, "_matches", return_value=[]), patch.object(backend, "_api") as api:
            with self.assertRaises(PublicationError): backend.reconcile_issue("key")
            api.assert_not_called()


if __name__ == "__main__": unittest.main()
