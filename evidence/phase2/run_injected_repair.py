"""Explicit test-double demonstration, separate from the nine live model calls.

Git isolation, regression commands, parallel build checks, review routing and local
PR creation execute for real. Inference responses deliberately use RejectOnce.
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from graph_engineering.centre_data import ticket_detail
from graph_engineering.runtime import digest
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import Workflow, initialise
from tests.test_core import make_repo
from tests.test_control_recovery import RejectOnce


def main():
    home = ROOT / '.runtime' / 'general-ai-agent'
    root = ROOT / '.runtime' / 'phase2-repair-injection'
    root.mkdir(exist_ok=True)
    repo = root / 'repo'
    if not repo.exists(): repo = make_repo(root)
    config_path = initialise(repo, home, trusted_local=True, routing='multimodel')
    config = json.loads(config_path.read_text())
    config.update(parallel_quality=True, build_commands=[[sys.executable,'-c',
        "import time;time.sleep(.2);print('Controlled fixture build verification')"]],
        demo_notice='Failure injection: scripted model responses force the first review to request changes. Git, test, queue and local PR receipts are real.')
    config_path.write_text(json.dumps(config,indent=2),encoding='utf-8')
    run_id = 'phase2-injected-repair'
    flow = Workflow(home)
    try:
        try: run = flow.store.get(run_id)
        except KeyError: run = flow.store.create_run(config,run_id)
        flow.worker = RejectOnce()
        flow.store.event(run_id,'independent_review','failure_injection_declared',{'notice':config['demo_notice']})
        with patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True}):
            result = flow.execute(run_id)
        assert result['status']=='waiting_approval', result['state'].get('error')
        assert result['state']['repairs']==1
    finally: flow.store.close()
    tickets = Tickets(home)
    try:
        ticket = tickets.admit(run_id,fingerprint=digest(['labelled-injected-repair',run_id]))
        tickets.command(ticket['id'],'edit',{'labels':['failure-injection','scripted-model-responses']},'injected-repair-labels')
    finally: tickets.store.close()
    detail = ticket_detail(home,ticket['id'])
    (ROOT/'evidence/phase2/injected-repair.json').write_text(json.dumps(
        {'notice':config['demo_notice'],'detail':detail},indent=2),encoding='utf-8')
    print(json.dumps({'ticket_id':ticket['id'],'run_id':run_id,'notice':config['demo_notice']}))


if __name__=='__main__': main()
