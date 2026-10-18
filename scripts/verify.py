"""Capture observed test and compile results without PowerShell stream decoration."""
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
output = root / "evidence/verification"
output.mkdir(parents=True, exist_ok=True)
import os
env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1")
receipts = []
for name, argv in [("unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
                   ("compile", [sys.executable, "-m", "compileall", "-q", "src", "scripts"])]:
    start = time.monotonic()
    result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    receipt = {"check": name, "argv": argv, "returncode": result.returncode, "stdout": result.stdout,
               "stderr": result.stderr, "duration_seconds": time.monotonic()-start}
    (output / (name + ".json")).write_text(json.dumps(receipt, indent=2))
    receipts.append(receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k not in ("stdout", "stderr")}))
    if result.returncode:
        print(result.stderr)
        raise SystemExit(result.returncode)
