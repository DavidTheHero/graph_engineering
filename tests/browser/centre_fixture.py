"""Labelled browser-test backend. Git/test/queue operations run for real.

Only model inference is replaced by WorkerDouble. Never used as live-model proof.
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

from graph_engineering.dashboard import make_server
from graph_engineering.control_repositories import register, start
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import Workflow, initialise
from tests.test_core import make_repo
from tests.test_workflow import WorkerDouble


class RejectOnce(WorkerDouble):
    def __init__(self):
        super().__init__()
        self.reviews = 0

    def invoke(self, role, *args, **kwargs):
        result = super().invoke(role, *args, **kwargs)
        if role == 'review':
            self.reviews += 1
            if self.reviews == 1:
                result['content'] = {'decision': 'request_changes', 'findings': [],
                                     'rationale': 'Explicit failure injection: exercise repair routing'}
        return result


def main():
    home = Path(sys.argv[1])
    repo = make_repo(home.parent)
    config = initialise(repo, home, trusted_local=True)
    settings=json.loads(config.read_text());settings['demo_notice']='Browser-test fixture: scripted inference responses; real Git and regression execution.';config.write_text(json.dumps(settings))
    flow = Workflow(home)
    try:
        flow.worker = RejectOnce()
        with patch('graph_engineering.workflow.ModelClient.health', return_value={'available': True}):
            run = flow.execute(flow.new_run(config)['id'])
        assert run['status'] == 'waiting_approval', run['state'].get('error')
        assert run['state']['repairs'] == 1
    finally:
        flow.store.close()
    tickets = Tickets(home)
    try:
        reviewed = tickets.admit(run['id'])
        registered = register(tickets, repo)
        queued = start(tickets, registered['id'], {'mode': 'read_only'}, 'browser-read-only')
        # A separate real run waits on the unapproved first run.
        pending = tickets.store.create_run({'repo': str(repo), 'backend': 'local', 'models': {}}, 'dependency-test')
        dependent = tickets.admit(pending['id'], dependencies=[reviewed['id']])
    finally:
        tickets.store.close()
    server = make_server(home, 0, control=True)
    print(json.dumps({'port': server.server_port, 'queued': queued['ticket_id'],
                      'reviewed': reviewed['id'], 'dependent': dependent['id']}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
