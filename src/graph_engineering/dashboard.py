"""Loopback-only, read-only workflow visualization server. No worker execution."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .dashboard_data import Catalog


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/graph.js": ("graph.js", "text/javascript; charset=utf-8"),
          "/details.js": ("details.js", "text/javascript; charset=utf-8"),
          "/dom.js": ("dom.js", "text/javascript; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


def make_server(home: Path, port: int = 8765, archive: Path | None = None):
    catalog = Catalog(home, archive)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            # Repository strings and request URLs never become terminal control sequences.
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, status, data):
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origins = {"http://" + h for h in hosts}
            if self.headers.get("Host") not in hosts or self.headers.get("Origin", "") not in origins | {""}:
                return self._json(403, {"error": "Only same-origin loopback access is allowed."})
            url = urlsplit(self.path)
            try:
                if url.path in ASSETS:
                    asset, content_type = ASSETS[url.path]
                    return self._send(200, files("graph_engineering").joinpath("web", asset).read_bytes(), content_type)
                if url.path == "/api/overview":
                    return self._json(200, catalog.overview())
                if url.path == "/api/run":
                    query = parse_qs(url.query, max_num_fields=8)
                    return self._json(200, catalog.detail(query.get("source", [""])[0], query.get("id", [""])[0]))
                return self._json(404, {"error": "Not found."})
            except KeyError:
                return self._json(404, {"error": "Run not found in this source."})
            except (sqlite3.Error, OSError, ValueError, TypeError):
                return self._json(503, {"error": "Execution data is unavailable or invalid. Check the selected controller and archive."})

        def do_POST(self):
            self._json(405, {"error": "This dashboard is read-only. Use the CLI for workflow actions."})

        do_PUT = do_PATCH = do_DELETE = do_POST

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_dashboard(home: Path, port: int = 8765):
    archive = Path(__file__).resolve().parents[2] / "evidence" / "final"
    with make_server(home, port, archive) as server:
        print(f"Workflow dashboard: http://127.0.0.1:{server.server_port}\nRead-only. Press Ctrl+C to stop.", flush=True)
        server.serve_forever(poll_interval=0.25)
