"""Foreground entry point for an OS-supervised controller, with retained logs."""
import argparse
from pathlib import Path
import sys
from graph_engineering.cli import main

parser=argparse.ArgumentParser();parser.add_argument('--home',type=Path,required=True)
parser.add_argument('--port',type=int,default=8771);parser.add_argument('--workers',type=int,default=2)
args=parser.parse_args();args.home.mkdir(parents=True,exist_ok=True)
stream=(args.home/'controller-service.log').open('a',encoding='utf-8',buffering=1)
sys.stdout=sys.stderr=stream
from graph_engineering.windows_job import contain_descendants
controller_job=contain_descendants()
sys.argv=['graph-engineering','--home',str(args.home.resolve()),'centre','--port',str(args.port),'--workers',str(args.workers)]
raise SystemExit(main())
