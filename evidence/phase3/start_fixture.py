"""Actual Phase 3 inference against controlled, deliberately defective Git code."""
import json
from pathlib import Path
import uuid
from urllib.request import Request,urlopen

from graph_engineering.evaluation import seed_repositories
from graph_engineering.workflow import initialise
from graph_engineering.tickets import Tickets
from graph_engineering.control_repositories import register

ROOT=Path(__file__).resolve().parents[2]
HOME=ROOT/'.runtime/general-ai-agent'
URL='http://127.0.0.1:8771'


def post(route,data):
    session=json.load(urlopen(URL+'/api/session'))
    return json.load(urlopen(Request(URL+'/api/centre/'+route,data=json.dumps(data).encode(),headers={
        'Origin':URL,'X-GE-CSRF':session['csrf'],'Idempotency-Key':'phase3-'+uuid.uuid4().hex,
        'Content-Type':'application/json'}),timeout=30))


def main():
    root=ROOT/'.runtime/phase3-fixtures'
    if not root.exists():seed_repositories(root)
    source=root/'python-invoice'
    initialise(source,HOME,trusted_local=True,routing='multimodel')
    tickets=Tickets(HOME)
    try:
        registered=register(tickets,source)
        with tickets.db:tickets.db.execute('UPDATE registered_repositories SET label=? WHERE id=?',('Phase 3 seeded invoice defect',registered['id']))
    finally:tickets.store.close()
    result=post('create-ticket',{'repository_id':registered['id'],
        'description':'When I apply a 10 percent discount to a 200 pound bill, I get almost the full amount instead of 180 pounds. Please investigate.',
        'reproduce':True,'trust_execution':True})
    (ROOT/'evidence/phase3/seeded-ticket-start.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
