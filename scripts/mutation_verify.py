"""Verify a real security test catches a deliberately inverted trust gate; restore bytes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
source = root / "src/graph_engineering/repository.py"
original = source.read_bytes()
needle, replacement = b"    if not trusted:\n", b"    if trusted:\n"
if original.count(needle) != 1: raise RuntimeError("Trust gate mutation target is not unique")
env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1")
argv = [sys.executable, "-m", "unittest", "tests.test_core.CoreTests.test_untrusted_code_is_never_executed", "-v"]
try:
    source.write_bytes(original.replace(needle, replacement, 1))
    mutant = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=20)
finally:
    source.write_bytes(original)
restored = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=20)
receipt = {"mutation": "Invert explicit repository-code trust gate", "argv": argv,
           "original_sha256": hashlib.sha256(original).hexdigest(),
           "restored_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
           "mutant_returncode": mutant.returncode, "mutant_output": mutant.stdout + mutant.stderr,
           "restored_returncode": restored.returncode, "restored_output": restored.stdout + restored.stderr}
(root / "evidence/verification/mutation.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
if mutant.returncode == 0 or restored.returncode != 0 or source.read_bytes() != original:
    raise RuntimeError("Security mutation verification failed")
print(json.dumps({"mutant_caught": True, "restored_test_passed": True, "exact_source_bytes_restored": True}))
