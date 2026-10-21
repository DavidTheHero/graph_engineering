"""The control centre API, the MCP tools and the CLI, on a real store."""
import http.client
import io
import json
import threading
import unittest
from contextlib import redirect_stdout

from graph_engineering import cli, mcp_server, web, workflow
from graph_engineering.repos import Repo, Repos
from graph_engineering.store import Store

from helpers import FakeLLM, TEST_COMMAND, python_repo, temp_dir


class Interfaces(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.home = self.root / "home"
        self.store, self.repos = Store(self.home / "ge.db"), Repos(self.home)
        self.repos.save(Repo(name="calc", path=str(python_repo(self.root)), test_command=TEST_COMMAND))
        llm = FakeLLM(lambda role, messages: "unused")
        self.addCleanup(llm.close)
        self.ctx = workflow.Context(self.home, self.store, self.repos, llm.registry())

    def server(self):
        server = web.ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = server.server_port
        server.RequestHandlerClass = web.make_handler(self.ctx, None, port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return port

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        base = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        connection.request(method, path, json.dumps(body) if body is not None else None, {**base, **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")

    def test_web_files_a_ticket_and_acts_on_it(self):
        port = self.server()
        status, ticket = self.request(port, "POST", "/api/tickets", {"repo": "calc", "request": "add subtract"}, {"X-GE": "1"})
        self.assertEqual((status, ticket["state"]), (201, "new"))
        status, state = self.request(port, "GET", "/api/state")
        self.assertEqual(state["tickets"][0]["id"], ticket["id"])
        status, error = self.request(port, "POST", f"/api/tickets/{ticket['id']}/accept", {}, {"X-GE": "1"})
        self.assertEqual(status, 409)  # a new ticket has no criteria yet: the intake agent comes first
        status, cancelled = self.request(port, "POST", f"/api/tickets/{ticket['id']}/cancel", {"text": "test"}, {"X-GE": "1"})
        self.assertEqual(cancelled["state"], "cancelled")

    def test_web_refuses_cross_site_and_foreign_hosts(self):
        port = self.server()
        status, _ = self.request(port, "POST", "/api/tickets", {"repo": "calc", "request": "x"})
        self.assertEqual(status, 403)  # no X-GE header: a plain cross-site form
        status, _ = self.request(port, "POST", "/api/tickets", {"repo": "calc", "request": "x"},
                                 {"X-GE": "1", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request(port, "GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 421)  # DNS rebinding
        self.assertEqual(self.store.list(), [])

    def test_web_approval_must_name_the_pr_head(self):
        port = self.server()
        ticket = self.store.create("calc", "x", "p", spec={"title": "t", "acceptance_criteria": ["a"]}, state="proposed")
        for to in ("accepted", "ready"):
            self.store.transition(ticket["id"], to, "p", to)
        self.store.transition(ticket["id"], "awaiting_merge", "p", "published", data={"pr": {"head": "a" * 40, "branch": "b"}})
        status, _ = self.request(port, "POST", f"/api/tickets/{ticket['id']}/approve-merge", {"head": "b" * 40}, {"X-GE": "1"})
        self.assertEqual(status, 409)
        status, ticket = self.request(port, "POST", f"/api/tickets/{ticket['id']}/approve-merge", {"head": "a" * 40}, {"X-GE": "1"})
        self.assertEqual((status, ticket["state"]), (200, "merging"))

    def test_mcp_tools_file_and_read_but_cannot_approve(self):
        functions = mcp_server.tools(self.store, self.repos)
        self.assertEqual(set(functions), {"list_repositories", "create_ticket", "list_tickets", "get_ticket", "ticket_activity"})
        ticket = functions["create_ticket"]("calc", "add subtract")
        self.assertEqual(functions["get_ticket"](ticket["id"])["request"], "add subtract")
        self.assertEqual(self.store.events(ticket["id"])[0]["actor"], "mcp")

    def test_cli_registers_a_repository_and_files_a_ticket(self):
        home = self.root / "cli-home"
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(home), "init"]), 0)
            self.assertEqual(cli.main(["--home", str(home), "repo", "add", "calc", self.repos.get("calc").path,
                                       "--test", TEST_COMMAND, "--local"]), 0)
            self.assertEqual(cli.main(["--home", str(home), "ticket", "new", "calc", "add subtract"]), 0)
            self.assertEqual(cli.main(["--home", str(home), "ticket", "list"]), 0)
        self.assertIn("add subtract", out.getvalue())
        self.assertIsNone(Repos(home).get("calc").slug)


if __name__ == "__main__":
    unittest.main()
