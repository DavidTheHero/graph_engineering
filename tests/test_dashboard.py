import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from graph_engineering.runtime import Store


class DashboardFixtures(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.folder.cleanup()

    def catalog(self):
        from graph_engineering.dashboard_data import Catalog
        return Catalog(self.root / "controller", self.root / "archive")

    def seed(self):
        store = Store(self.root / "controller")
        self.stores.append(store)
        store.create_run({"repo": "D:/sample/python-invoice", "backend": "local",
                          "api_key": "never-send-this", "models": {}}, "run1")
        return store


class DashboardTests(DashboardFixtures):
    def test_empty_home_does_not_create_database(self):
        result = self.catalog().overview()
        self.assertEqual(result["runs"], [])
        self.assertFalse((self.root / "controller").exists())

    def test_live_refresh_reads_new_checkpoints_and_excludes_configuration(self):
        store = self.seed()
        catalog = self.catalog()
        self.assertEqual(catalog.overview()["runs"][0]["status"], "ready")
        store.checkpoint("run1", "repository_discovery", {"base_sha": "a" * 40}, "evidence_collection")
        result = catalog.detail("live", "run1")
        self.assertEqual(result["run"]["node"], "evidence_collection")
        self.assertEqual(result["nodes"]["repository_discovery"]["status"], "completed")
        self.assertEqual(result["nodes"]["evidence_collection"]["status"], "ready")
        self.assertNotIn("never-send-this", json.dumps(result))
        self.assertNotIn("config", result["evidence"])

    def test_skipped_nodes_are_not_invented_successes_and_failures_recover(self):
        store = self.seed()
        store.event("run1", "evidence_collection", "failed", {"message": "retry"})
        store.checkpoint("run1", "evidence_collection", {"baseline": {"passed": True}}, "completion")
        store.checkpoint("run1", "completion", {"outcome": "no_findings"}, "done", "no_findings")
        detail = self.catalog().detail("live", "run1")
        self.assertEqual(detail["nodes"]["issue_detection"]["status"], "not_run")
        self.assertEqual(detail["nodes"]["evidence_collection"]["status"], "completed")
        self.assertEqual(detail["nodes"]["evidence_collection"]["failures"], 1)

    def test_failed_tests_and_blocked_cursor_are_not_shown_as_passes(self):
        store = self.seed()
        store.checkpoint("run1", "test_execution", {"verification": {"passed": False}}, "repair")
        result = self.catalog().detail("live", "run1")
        self.assertEqual(result["nodes"]["test_execution"]["status"], "needs_repair")
        store.checkpoint("run1", "ci_policy_verification", {"error": "403 denied"}, "ci_policy_verification", "blocked")
        result = self.catalog().detail("live", "run1")
        self.assertEqual(result["nodes"]["ci_policy_verification"]["status"], "blocked")
        self.assertEqual(result["run"]["attention"], "403 denied")

    def test_secrets_are_redacted_and_logs_are_bounded(self):
        store = self.seed()
        with patch.dict("os.environ", {"UI_TEST_TOKEN": "redact-sensitive-value"}):
            store.checkpoint("run1", "test_execution", {"diff": "x" * 90000,
                "error": "redact-sensitive-value ghp_abcdef123456789"}, "test_execution", "blocked")
            payload = json.dumps(self.catalog().detail("live", "run1"))
        self.assertNotIn("redact-sensitive-value", payload)
        self.assertNotIn("ghp_abcdef", payload)
        self.assertIn("truncated", payload)
        self.assertLess(len(payload), 110000)

    def test_event_history_is_not_silently_cut_to_one_hundred(self):
        store = self.seed()
        for number in range(120):
            store.event("run1", "repository_discovery", "observation", {"number": number})
        result = self.catalog().detail("live", "run1")
        self.assertEqual(len(result["events"]), 121)
        self.assertFalse(result["events_truncated"])

    def test_graph_uses_transitions_after_the_display_history_limit(self):
        store = self.seed()
        for number in range(2001):
            store.event("run1", "repository_discovery", "observation", {"number": number})
        store.checkpoint("run1", "test_execution", {"verification": {"passed": True}}, "completion")
        store.checkpoint("run1", "completion", {"outcome": "completed"}, "done", "completed")
        result = self.catalog().detail("live", "run1")
        self.assertTrue(result["events_truncated"])
        self.assertEqual(len(result["events"]), 2000)
        self.assertEqual(result["nodes"]["test_execution"]["status"], "completed")

    def test_archive_is_labelled_and_path_inputs_cannot_escape_catalog(self):
        archive = self.root / "archive"
        archive.mkdir()
        (archive / "public-run.json").write_text(json.dumps({"id": "historical1", "repo": "fixture",
            "node": "done", "status": "completed", "state": {"config": {"backend": "github"}}}))
        (archive / "public-events.json").write_text("[]")
        catalog = self.catalog()
        self.assertEqual(catalog.overview()["runs"][0]["source_kind"], "recorded")
        self.assertEqual(catalog.detail("recorded", "historical1")["run"]["backend"], "github")
        for source, run in [("../archive", "historical1"), ("live", "../state.sqlite3")]:
            with self.assertRaises(KeyError):
                catalog.detail(source, run)


class DashboardHTTPTests(DashboardFixtures):
    def setUp(self):
        super().setUp()
        from graph_engineering.dashboard import make_server
        self.server = make_server(self.root / "controller", port=0, archive=self.root / "archive")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def test_http_assets_api_and_security_boundary(self):
        with urlopen(self.url) as response:
            self.assertIn(b"Workflow", response.read())
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with urlopen(self.url + "/api/overview") as response:
            self.assertEqual(json.load(response)["runs"], [])
        for path in ["/../.env", "/api/run?source=live&id=../secret", "/state.sqlite3"]:
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.url + path)
            self.assertEqual(caught.exception.code, 404)
        for headers in [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}]:
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(self.url + "/api/overview", headers=headers))
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(self.url + "/api/approve", data=b"{}", method="POST"))
        self.assertEqual(caught.exception.code, 405)

    def test_database_error_is_visible_without_exposing_paths(self):
        (self.root / "controller").mkdir()
        (self.root / "controller" / "state.sqlite3").write_text("broken database")
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + "/api/overview")
        self.assertEqual(caught.exception.code, 503)
        self.assertNotIn(str(self.root), caught.exception.read().decode())


if __name__ == "__main__":
    unittest.main()
