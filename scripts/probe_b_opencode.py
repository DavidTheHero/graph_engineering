"""Capture the actual native-provider request in a disposable localhost probe."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import urllib.request
import urllib.error
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from graph_engineering.models import OpenCodeWorker, default_models

root = Path(__file__).resolve().parents[1] / "evidence/connectivity/b8k-native-request"
root.mkdir(parents=True, exist_ok=True)
requests = []
class Proxy(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        requests.append(json.loads(body))
        upstream = urllib.request.Request("http://192.168.0.62:18035" + self.path, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(upstream, timeout=60) as response:
                payload = response.read(2_000_000)
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *_): pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
threading.Thread(target=server.serve_forever, daemon=True).start()
models = default_models()
models["b"]["endpoint"] = f"http://127.0.0.1:{server.server_port}/v1"
schema = {"type":"object","properties":{"language":{"type":"string"}},"required":["language"],"additionalProperties":False}
try:
    result = OpenCodeWorker(root, models).invoke("investigation", 'Return JSON {"language":"python"}.', schema, "b", "probe")
    print(json.dumps(result))
except Exception as exc:
    print(type(exc).__name__, str(exc))
finally:
    (root / "request.json").write_text(json.dumps(requests, indent=2), encoding="utf-8")
    print(json.dumps(requests, indent=2))
    server.shutdown()
