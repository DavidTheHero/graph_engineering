"""Test fixtures: a scripted OpenAI-compatible server and a tiny Python repository."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading

from graph_engineering.llm import Model, Registry


def call(name: str, **arguments) -> dict:
    return {"name": name, "arguments": arguments}


class FakeLLM:
    """Serves scripted replies. ``script(role, messages)`` returns a list of tool calls, a string, or a dict reply."""

    def __init__(self, script):
        self.script, self.requests = script, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                system = body["messages"][0]["content"]
                role = ("intake" if "turn a request" in system else "review" if "You review a change" in system
                        else "plan" if "You plan a change" in system else "plan_review" if "You check a plan" in system
                        else "explore" if "You answer one question" in system else "coding")
                reply = fake.script(role, body["messages"])
                if isinstance(reply, str):
                    message, finish = {"role": "assistant", "content": reply}, "stop"
                elif isinstance(reply, dict):
                    message, finish = reply["message"], reply.get("finish_reason", "stop")
                else:
                    message = {"role": "assistant", "content": "", "tool_calls": [
                        {"id": f"c{len(fake.requests)}_{i}", "type": "function",
                         "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for i, c in enumerate(reply)]}
                    finish = "tool_calls"
                payload = json.dumps({"choices": [{"message": message, "finish_reason": finish}],
                                      "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"

    def model(self, name: str = "fake") -> Model:
        return Model(name=name, endpoint=self.endpoint, model_id="fake", context=32768, max_output=1024, timeout=10)

    def registry(self) -> Registry:
        return Registry({"fake": self.model()}, {})

    def close(self):
        self.server.shutdown()


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def python_repo(root: Path) -> Path:
    """A git repository with calc.add and a unittest suite."""
    repo = root / "calc"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_add.py").write_text(
        "import unittest\nfrom calc import add\n\n\nclass Add(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


SUB_TEST = ("import unittest\nfrom calc import subtract\n\n\nclass Subtract(unittest.TestCase):\n"
            "    def test_subtract(self):\n        self.assertEqual(subtract(5, 3), 2)\n")
SUB_SOURCE = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
TEST_COMMAND = "python3 -m unittest discover -s tests -q"


def temp_dir(case) -> Path:
    handle = tempfile.TemporaryDirectory()
    case.addCleanup(handle.cleanup)
    return Path(handle.name).resolve()
