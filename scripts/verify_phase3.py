"""Repeat backend/browser acceptance checks with separate, observed receipts."""
import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=root/'evidence/phase3');args=parser.parse_args()
output=args.output.resolve()
output.mkdir(parents=True,exist_ok=True)
checks=[('unittest',[sys.executable,'-m','unittest','discover','-s','tests','-v'],root),
        ('compile',[sys.executable,'-m','compileall','-q','src','scripts'],root),
        ('intake-browser',['node','intake.cjs'],root/'tests/browser'),
        ('centre-browser',['node','centre.cjs'],root/'tests/browser'),
        ('legacy-browser',['node','dashboard.cjs'],root/'tests/browser'),
        ('motion',['node','motion.cjs'],root/'tests/browser')]
for name,argv,cwd in checks:
    env=dict(os.environ,PYTHONPATH=str(root/'src'),PYTHONDONTWRITEBYTECODE='1',
             GE_BROWSER_OUTPUT=str(output/name))
    start=time.monotonic()
    result=subprocess.run(argv,cwd=cwd,env=env,capture_output=True,text=True,
                          encoding='utf-8',errors='replace',timeout=180)
    receipt={'check':name,'argv':argv,'returncode':result.returncode,
             'duration_seconds':time.monotonic()-start,'stdout':result.stdout,'stderr':result.stderr}
    (output/(name+'.json')).write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(name+': '+str(result.returncode),flush=True)
    if result.returncode:
        print(result.stdout+result.stderr)
        raise SystemExit(result.returncode)
