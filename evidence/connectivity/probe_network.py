import json
from pathlib import Path
import socket
import time
import urllib.request

record = {"proxy_configured": bool(urllib.request.getproxies()), "host": "192.168.0.62", "port": 18035}
start = time.monotonic()
try:
    with socket.create_connection((record["host"], record["port"]), timeout=3):
        record["tcp_connected"] = True
except Exception as exc:
    record.update(tcp_connected=False, error=type(exc).__name__ + ": " + str(exc))
record["latency_seconds"] = round(time.monotonic()-start,4)
Path(__file__).with_name("b_network.json").write_text(json.dumps(record,indent=2),encoding="utf-8")
print(json.dumps(record))
