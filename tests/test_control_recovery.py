"""Real process concurrency/recovery, admission and labelled repair injection."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from graph_engineering.control_repositories import register, start
from graph_engineering.scheduler import claim, recover
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import Workflow, initialise
from graph_engineering.models import ModelError
from graph_engineering.runtime import RepoLock
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
                                     'rationale': 'Labelled failure injection: exercise repair routing'}
        return result


class ControlRecoveryTests(unittest.TestCase):
    def test_context_routing_cannot_bypass_actual_model_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo=make_repo(root);home=root/'controller'
            config=initialise(repo,home,trusted_local=True,routing='multimodel')
            flow=Workflow(home)
            try:
                run=flow.new_run(config);flow.run_id=run['id'];flow.worker=WorkerDouble()
                with RepoLock(home,'inference-model:a'),self.assertRaises(ModelError):
                    flow.ask(run['state'],'investigation',{'task':'x'*7000},{'type':'object'},'b')
                self.assertEqual(flow.worker.metrics,[])
                kinds=[e['kind'] for e in flow.store.events(run['id'])]
                self.assertIn('degraded_routing',kinds)
                self.assertIn('model_capacity_wait',kinds)
                self.assertNotIn('model_assigned',kinds)
            finally:flow.store.close()

    def test_concurrent_process_claims_respect_global_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            tickets = Tickets(home)
            try:
                for n in range(6):
                    tickets.store.create_run({'repo': str(home / str(n)), 'backend': 'local'}, str(n))
                    tickets.admit(str(n))
                script = ('import json,sys;from pathlib import Path;from graph_engineering.tickets import Tickets;'
                          'from graph_engineering.scheduler import claim;t=Tickets(Path(sys.argv[1]));'
                          'a=claim(t,2);print(json.dumps(a["id"] if a else None));t.store.close()')
                workers = [subprocess.Popen([sys.executable, '-c', script, str(home)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
                selected = []
                for worker in workers:
                    out, err = worker.communicate(timeout=20)
                    self.assertEqual(worker.returncode, 0, err)
                    selected.append(json.loads(out))
                self.assertEqual(len([a for a in selected if a]), 2)
                self.assertEqual(len(set(a for a in selected if a)), 2)
                self.assertEqual(sum(bool(t['lease_owner']) for t in tickets.list()), 2)
            finally:
                tickets.store.close()

    def test_controller_recovery_preserves_live_worker_then_fences_killed_worker(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            tickets = Tickets(home)
            worker = None
            try:
                tickets.store.create_run({'repo': str(home / 'source'), 'backend': 'local'}, 'run')
                ticket = tickets.admit('run'); assigned = claim(tickets)
                script = ('import sys,time;from pathlib import Path;from graph_engineering.runtime import RepoLock;'
                          'lock=RepoLock(Path(sys.argv[1]),"ticket:"+sys.argv[2]);lock.__enter__();'
                          'print("locked",flush=True);time.sleep(30)')
                worker = subprocess.Popen([sys.executable, '-u', '-c', script, str(home), ticket['id']],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual(worker.stdout.readline().strip(), 'locked')
                recover(tickets)
                self.assertEqual(tickets.get(ticket['id'])['lease_owner'], assigned['lease_owner'])
                worker.kill(); worker.communicate(timeout=5)
                recover(tickets)
                restored = tickets.get(ticket['id'])
                self.assertIsNone(restored['lease_owner'])
                self.assertEqual(restored['node'], 'repository_discovery')
                self.assertEqual(tickets.queue(restored)[0], 'retry')
            finally:
                if worker and worker.poll() is None:
                    worker.kill(); worker.communicate(timeout=5)
                tickets.store.close()

    def test_admission_deduplicates_before_creating_an_orphan_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = make_repo(root); tickets = Tickets(root / 'controller')
            try:
                registered = register(tickets, repo)
                first = start(tickets, registered['id'], {'mode': 'read_only'}, 'admission-first')
                second = start(tickets, registered['id'], {'mode': 'read_only'}, 'admission-second')
                self.assertEqual(first, second)
                self.assertEqual(tickets.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)
                with self.assertRaises(ValueError):
                    start(tickets, registered['id'], {'mode':'read_only','dependencies':['unknown']}, 'admission-invalid')
                self.assertEqual(tickets.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)
                model_scan = start(tickets, registered['id'], {'mode': 'read_only', 'inspect_with_model': True}, 'admission-model')
                self.assertNotEqual(first['run_id'], model_scan['run_id'])
                self.assertEqual(len(tickets.list()), 2)
            finally:
                tickets.store.close()

    @patch('graph_engineering.workflow.ModelClient.health', return_value={'available': True})
    def test_review_failure_executes_repair_regression_and_review_without_duplicate_publication(self, _):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); repo = make_repo(root)
            config = initialise(repo, root / 'controller', trusted_local=True)
            flow = Workflow(root / 'controller')
            try:
                flow.worker = RejectOnce()
                run = flow.execute(flow.new_run(config)['id'])
                self.assertEqual(run['status'], 'waiting_approval', run['state'].get('error'))
                self.assertEqual(run['state']['repairs'], 1)
                stages = [e['node'] for e in flow.store.events(run['id']) if e['kind'] == 'started']
                review = stages.index('independent_review')
                self.assertEqual(stages[review:review+5], ['independent_review','repair',
                    'isolated_implementation','test_execution','independent_review'])
                backend = flow.backend(run['state'])
                flow.execute(run['id'])
                objects = json.loads(backend.path.read_text())
                self.assertEqual(len(objects['issues']), 1)
                self.assertEqual(len(objects['prs']), 1)
                self.assertFalse(run['state']['gate']['allowed'])
            finally:
                flow.store.close()
