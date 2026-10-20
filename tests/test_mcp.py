"""MCP protocol compliance and permission-boundary tests against the official SDK client."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

try:
    import anyio
    from mcp import Client, StdioServerParameters
    from graph_engineering.mcp_server import build_server, http_app
except ImportError:  # The MCP extra is optional; the core suite must not depend on it.
    Client = None

from graph_engineering.capabilities import Capabilities, InvalidArgument, PermissionDenied
from graph_engineering.control_repositories import register
from graph_engineering.tickets import Tickets
from tests.test_core import make_repo

ROOT = Path(__file__).resolve().parents[1]


class Fixture:
    def __init__(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.repo = make_repo(root)
        self.home = root / "home"
        tickets = Tickets(self.home)
        try:
            self.repository_id = register(tickets, str(self.repo))["id"]
        finally:
            tickets.store.close()

    def audit(self):
        tickets = Tickets(self.home)
        try:
            return [json.loads(r[0]) for r in tickets.db.execute("SELECT data FROM events WHERE kind='mcp_tool_call' ORDER BY seq")]
        finally:
            tickets.store.close()


class CapabilityBoundaryTests(unittest.TestCase):
    """Profile enforcement inside the capability layer, independent of the protocol adapter."""

    def setUp(self):
        self.fixture = Fixture()

    def tearDown(self):
        self.fixture.folder.cleanup()

    def test_read_profile_cannot_write_and_refusals_are_audited(self):
        caps = Capabilities(self.fixture.home, "read", "test")
        with self.assertRaises(PermissionDenied):
            caps.call("create_ticket", repository_id=self.fixture.repository_id, description="x", idempotency_key="k" * 10)
        self.assertEqual(self.fixture.audit()[-1]["outcome"], "permission_denied")

    def test_operate_cannot_approve_or_register(self):
        caps = Capabilities(self.fixture.home, "operate", "test")
        with self.assertRaises(InvalidArgument):
            caps.call("control_ticket", ticket_id="GE-00000000", action="approve", idempotency_key="k" * 10)
        with self.assertRaises(PermissionDenied):
            caps.call("register_repository", path=str(self.fixture.repo))
        self.assertFalse(hasattr(caps, "approve"))

    def test_ticket_creation_is_idempotent_and_matches_dashboard_path(self):
        caps = Capabilities(self.fixture.home, "operate", "test")
        args = dict(repository_id=self.fixture.repository_id, description="add() returns the wrong sum", idempotency_key="same-key-123")
        first, second = caps.call("create_ticket", **args), caps.call("create_ticket", **args)
        self.assertEqual(first, second)
        self.assertEqual(caps.call("list_tickets")["total"], 1)
        with self.assertRaisesRegex(InvalidArgument, "reused"):
            caps.call("create_ticket", **(args | {"description": "different"}))

    def test_revision_refs_cannot_inject_git_options(self):
        caps = Capabilities(self.fixture.home, "read", "test")
        for ref in ("--output=/tmp/x", "-p", "HEAD;rm"):
            with self.subTest(ref=ref), self.assertRaises(InvalidArgument):
                caps.call("read_diff", repository_id=self.fixture.repository_id, base=ref)
        with self.assertRaises(InvalidArgument):
            caps.call("git_history", repository_id=self.fixture.repository_id, path="../outside")

    def test_search_reads_committed_source_not_working_tree(self):
        (self.fixture.repo / "calc.py").write_text("UNCOMMITTED_SECRET_MARKER = 1\n")
        caps = Capabilities(self.fixture.home, "read", "test")
        found = caps.call("search_repository", repository_id=self.fixture.repository_id, terms=["UNCOMMITTED_SECRET_MARKER"])
        self.assertNotIn("UNCOMMITTED_SECRET_MARKER", json.dumps(found["excerpts"]))
        self.assertIn("Untrusted", found["trust"])


@unittest.skipIf(Client is None, "optional mcp extra not installed")
class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def tearDown(self):
        self.fixture.folder.cleanup()

    def exercise(self, client):
        async def body():
            tools = {t.name: t for t in (await client.list_tools()).tools}
            inspected = await client.call_tool("inspect_repository", {"repository_id": self.fixture.repository_id})
            graph = json.loads((await client.read_resource("graph-engineering://graph")).contents[0].text)
            templates = [t.uri_template for t in (await client.list_resource_templates()).resource_templates]
            missing = await client.call_tool("get_run", {"run_id": "doesnotexist"})
            return tools, inspected, graph, templates, missing, client.protocol_version
        return body()

    def check(self, tools, inspected, graph, templates, missing, protocol):
        self.assertTrue(protocol)
        self.assertIn("search_repository", tools)
        self.assertNotIn("create_ticket", tools)  # Read profile lists no write tools at all.
        self.assertTrue(tools["search_repository"].annotations.read_only_hint)
        self.assertIn("terms", tools["search_repository"].input_schema["properties"])
        self.assertFalse(inspected.is_error)
        self.assertEqual(inspected.structured_content["discovery"]["adapter"], "python")
        self.assertIn("repository_discovery", {n["name"] for n in graph["nodes"]})
        self.assertIn("graph-engineering://tickets/{ticket_id}", templates)
        self.assertTrue(missing.is_error)
        self.assertIn("not_found", missing.content[0].text)

    def test_in_process_session(self):
        async def main():
            async with Client(build_server(self.fixture.home, "read", "test")) as client:
                return await self.exercise(client)
        self.check(*anyio.run(main))
        self.assertTrue(any(a["tool"] == "inspect_repository" and a["outcome"] == "ok" for a in self.fixture.audit()))

    def test_stdio_subprocess_via_cli_and_older_protocol_negotiation(self):
        params = StdioServerParameters(command=sys.executable, args=["-m", "graph_engineering", "--home", str(self.fixture.home),
                                                                     "mcp", "serve", "--client", "stdio-test"],
                                       env={"PYTHONPATH": str(ROOT / "src"), "PATH": os.environ["PATH"]})

        async def main():
            async with Client(params) as client:
                return await self.exercise(client)
        self.check(*anyio.run(main))
        # Raw JSON-RPC initialize with an older protocol version must be negotiated, not rejected.
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "raw", "version": "0"}}}
        result = subprocess.run([sys.executable, "-m", "graph_engineering", "--home", str(self.fixture.home), "mcp", "serve"],
                                input=json.dumps(request) + "\n", capture_output=True, text=True, timeout=60,
                                env={"PYTHONPATH": str(ROOT / "src"), "PATH": os.environ["PATH"]})
        response = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["serverInfo"]["name"], "graph-engineering")

    def test_operate_profile_creates_ticket_over_protocol(self):
        async def main():
            async with Client(build_server(self.fixture.home, "operate", "test")) as client:
                created = await client.call_tool("create_ticket", {"repository_id": self.fixture.repository_id,
                    "description": "add() subtracts instead of adding", "idempotency_key": "proto-key-1"})
                denied = await client.call_tool("control_ticket", {"ticket_id": created.structured_content["ticket_id"],
                    "action": "approve", "idempotency_key": "proto-key-2"})
                listed = await client.call_tool("list_tickets", {})
                return created, denied, listed
        created, denied, listed = anyio.run(main)
        self.assertFalse(created.is_error)
        self.assertTrue(denied.is_error)
        self.assertEqual(listed.structured_content["total"], 1)

    def test_http_transport_requires_token_and_loopback_host(self):
        token = "t" * 32
        with self.assertRaises(ValueError):
            http_app(self.fixture.home, "read", "http", "short")
        import uvicorn
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(http_app(self.fixture.home, "read", "http", token), host="127.0.0.1",
                                               port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            url = f"http://127.0.0.1:{port}/mcp"

            def status(headers):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "h", "version": "0"}}}).encode()
                request = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                                             "Accept": "application/json, text/event-stream", **headers})
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status
                except urllib.error.HTTPError as exc:
                    return exc.code
            self.assertEqual(status({}), 401)
            self.assertEqual(status({"Authorization": "Bearer wrong"}), 401)
            self.assertEqual(status({"Authorization": "Bearer " + token, "Host": "evil.example"}), 401)
            self.assertEqual(status({"Authorization": "Bearer " + token}), 200)

            async def authenticated():
                import httpx2
                http = httpx2.AsyncClient(headers={"Authorization": "Bearer " + token})
                from mcp.client.streamable_http import streamable_http_client
                async with streamable_http_client(url, http_client=http) as streams:
                    from mcp import ClientSession
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        return [t.name for t in (await session.list_tools()).tools]
            try:
                names = anyio.run(authenticated)
            except ImportError:
                names = None
            if names is not None:
                self.assertIn("workflow_graph", names)
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
