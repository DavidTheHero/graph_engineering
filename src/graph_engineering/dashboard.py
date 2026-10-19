"""Loopback evidence viewer and explicitly enabled operator control centre."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .dashboard_data import Catalog


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/graph.js": ("graph.js", "text/javascript; charset=utf-8"),
          "/details.js": ("details.js", "text/javascript; charset=utf-8"),
          "/dom.js": ("dom.js", "text/javascript; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
for asset in ('centre.js','centre-state.js','centre-board.js','centre-network.js','centre-dialogs.js','centre-motion.js','centre-intake.js'):
    ASSETS['/'+asset] = (asset,'text/javascript; charset=utf-8')
ASSETS['/centre.css'] = ('centre.css','text/css; charset=utf-8')
ASSETS['/cytoscape-host.css'] = ('cytoscape-host.css','text/css; charset=utf-8')
ASSETS['/vendor/cytoscape.js'] = ('vendor/cytoscape.js','text/javascript; charset=utf-8')
ASSETS['/vendor/cytoscape.js.LEGAL.txt'] = ('vendor/cytoscape.js.LEGAL.txt','text/plain; charset=utf-8')
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

    def __init__(self,*args,**kwargs):
        self.stopping = threading.Event()
        super().__init__(*args,**kwargs)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def server_close(self):
        self.stopping.set()
        super().server_close()


def make_server(home: Path, port: int = 8765, archive: Path | None = None, control: bool = False):
    catalog = Catalog(home, archive)
    csrf = secrets.token_urlsafe(32)
    streams = threading.BoundedSemaphore(8)
    if control:
        from .tickets import Tickets
        from .control_repositories import import_configurations
        tickets = Tickets(home)
        try:
            import_configurations(tickets)
            for run in tickets.store.list_runs(): tickets.classify(tickets.admit(run['id']))
        finally: tickets.store.close()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            # Repository strings and request URLs never become terminal control sequences.
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, TimeoutError):
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
                if url.path == '/api/session':
                    return self._json(200,{'control_enabled':control,'csrf':csrf if control else None})
                if url.path == '/api/centre':
                    from .centre_data import snapshot
                    return self._json(200,snapshot(home))
                if url.path == '/api/centre/ticket':
                    from .centre_data import ticket_detail
                    query = parse_qs(url.query,max_num_fields=4)
                    return self._json(200,ticket_detail(home,query.get('id',[''])[0]))
                if url.path == '/api/centre/history':
                    from .tickets import Tickets
                    from .centre_data import events
                    query = parse_qs(url.query,max_num_fields=4)
                    tickets = Tickets(home)
                    try: result = events(tickets,max(0,int(query.get('after',['0'])[0])),250)
                    finally: tickets.store.close()
                    return self._json(200,{'events':result})
                if url.path == '/api/centre/events':
                    return self._stream(parse_qs(url.query,max_num_fields=4))
                if url.path == '/' and control:
                    return self._send(200,files('graph_engineering').joinpath('web','centre.html').read_bytes(),'text/html; charset=utf-8')
                if url.path == '/legacy':
                    return self._send(200,files('graph_engineering').joinpath('web','index.html').read_bytes(),'text/html; charset=utf-8')
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
            if not control: return self._json(405, {"error": "This dashboard is read-only. Use the CLI for workflow actions."})
            hosts = {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            if (self.headers.get('Host') not in hosts or self.headers.get('Origin') not in {'http://'+h for h in hosts}
                    or not secrets.compare_digest(self.headers.get('X-GE-CSRF',''),csrf)):
                return self._json(403,{'error':'Same-origin operator session and CSRF header required.'})
            try:
                size = int(self.headers.get('Content-Length','0'))
                limit=96000 if urlsplit(self.path).path=='/api/centre/create-ticket' else 16384
                if not 0 < size <= limit: raise ValueError(f'Command body must be between 1 and {limit} bytes')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data,dict): raise ValueError('Command must be a JSON object')
                from .control_api import operate
                result = operate(home,urlsplit(self.path).path,data,self.headers.get('Idempotency-Key',''))
                return self._json(200,result)
            except KeyError: return self._json(404,{'error':'Unknown repository, ticket or command.'})
            except (ValueError,TypeError,RuntimeError,OSError) as exc:
                from .runtime import redact
                return self._json(409,redact({'error':str(exc)}))

        def _stream(self, query):
            try: after = max(0,int(query.get('after',['0'])[0]),int(self.headers.get('Last-Event-ID','0')))
            except ValueError: return self._json(400,{'error':'Invalid event cursor'})
            if not streams.acquire(blocking=False): return self._json(429,{'error':'Event stream capacity reached'})
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            from .tickets import Tickets
            from .centre_data import events
            tickets = Tickets(home)
            try:
                deadline = time.monotonic()+25
                while time.monotonic()<deadline and not self.server.stopping.is_set():
                    batch = events(tickets,after)
                    for event in batch:
                        payload = f"id: {event['event_id']}\nevent: execution\ndata: {json.dumps(event,ensure_ascii=False)}\n\n"
                        self.wfile.write(payload.encode('utf-8')); after = event['event_id']
                    if not batch: self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush(); self.server.stopping.wait(.4)
            except (BrokenPipeError,ConnectionResetError,OSError): pass
            finally:
                tickets.store.close(); streams.release()

        do_PUT = do_PATCH = do_DELETE = do_POST

    return DashboardServer(("127.0.0.1", port), Handler)


def serve_dashboard(home: Path, port: int = 8765):
    archive = Path(__file__).resolve().parents[2] / "evidence" / "final"
    with make_server(home, port, archive) as server:
        print(f"Workflow dashboard: http://127.0.0.1:{server.server_port}\nRead-only. Press Ctrl+C to stop.", flush=True)
        server.serve_forever(poll_interval=0.25)


def serve_centre(home: Path, port=8765, workers=2, attach_controller=False):
    home = Path(home).resolve()
    with make_server(home,port,control=True) as server:
        kwargs = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session':True}
        controller = None
        if not attach_controller:
            with (home/'scheduler.log').open('ab') as output:
                controller = subprocess.Popen([sys.executable,'-m','graph_engineering','--home',str(home),
                    'dispatch','--workers',str(workers)],stdin=subprocess.DEVNULL,stdout=output,stderr=output,**kwargs)
        service = f'Controller PID: {controller.pid}' if controller else 'Attached to existing dispatch service; check dashboard heartbeat'
        print(f'Control centre: http://127.0.0.1:{server.server_port}\n{service}; state: {home}',flush=True)
        try: server.serve_forever(poll_interval=.25)
        finally:
            # Active workers retain their own leases/locks and finish their step;
            # restarting the controller safely reconciles them.
            if controller and controller.poll() is None: controller.terminate()
