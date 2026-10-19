"""Export a redacted ticket, persisted timeline and actual worker receipts."""
import argparse
import json
from pathlib import Path

from graph_engineering.centre_data import ticket_detail

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('ticket')
parser.add_argument('--home',type=Path,default=Path('.runtime'))
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
detail=ticket_detail(args.home,args.ticket)
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(detail,indent=2,ensure_ascii=False),encoding='utf-8')
print(str(args.output.resolve()))
