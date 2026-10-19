"""Isolated browser backend; inference is explicitly injected by the test driver."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from graph_engineering.control_repositories import register
from graph_engineering.dashboard import make_server
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import initialise
from tests.test_core import make_repo

home=Path(sys.argv[1]);repo=make_repo(home.parent)
config=initialise(repo,home,trusted_local=True,routing='multimodel')
data=json.loads(config.read_text());data['demo_notice']='Phase 3 browser fixture: scripted inference, real Git, queues, validation and revisions.';config.write_text(json.dumps(data))
tickets=Tickets(home)
try:registered=register(tickets,repo)
finally:tickets.store.close()
server=make_server(home,0,control=True)
print(json.dumps({'port':server.server_port,'repository_id':registered['id']}),flush=True)
server.serve_forever()
