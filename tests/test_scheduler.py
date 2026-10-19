import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graph_engineering.scheduler import claim, recover, execute_assignment
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import Workflow, initialise
from tests.test_core import make_repo


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        self.tickets=Tickets(self.root/'controller')

    def tearDown(self):
        self.tickets.store.close();self.folder.cleanup()

    def add(self,name,repo=None,priority='P2'):
        run=self.tickets.store.create_run({'repo':str(repo or self.root/name),'backend':'local','models':{},'routing':'baseline'},name)
        return self.tickets.admit(run['id'],priority=priority)

    def test_atomic_priority_capacity_repository_and_dependency_admission(self):
        low=self.add('low',priority='P3');high=self.add('high',priority='P0')
        first=claim(self.tickets,2);self.assertEqual(first['id'],high['id'])
        second=claim(self.tickets,2);self.assertEqual(second['id'],low['id'])
        self.assertIsNone(claim(self.tickets,2))

    def test_same_repository_is_serialized_while_other_repository_continues(self):
        self.add('one',repo=self.root/'same');self.add('two',repo=self.root/'same');third=self.add('three')
        claim(self.tickets,2);self.assertEqual(claim(self.tickets,2)['id'],third['id'])
        waiting=self.tickets.get(next(t['id'] for t in self.tickets.list() if t['run_id']=='two'))
        self.assertIn('Repository',waiting['last_reason'])

    def test_worker_failure_recovery_preserves_cursor_and_fences_old_owner(self):
        t=self.add('one');claimed=claim(self.tickets)
        self.tickets.store.checkpoint('one','repository_discovery',{},'repository_discovery','running','started')
        recover(self.tickets)
        restored=self.tickets.get(t['id'])
        self.assertIsNone(restored['lease_owner']);self.assertEqual(restored['status'],'ready')
        self.assertEqual(execute_assignment(self.root/'controller',t['id'],claimed['lease_owner']),0)
        self.assertEqual(self.tickets.store.get('one')['node'],'repository_discovery')

    def test_one_real_graph_node_checkpoints_without_node_budget_failure(self):
        repo=make_repo(self.root)
        config=initialise(repo,self.root/'controller',trusted_local=True)
        flow=Workflow(self.root/'controller')
        try: run=flow.new_run(config)
        finally: flow.store.close()
        ticket=self.tickets.admit(run['id']);assigned=claim(self.tickets)
        code=execute_assignment(self.root/'controller',ticket['id'],assigned['lease_owner'])
        self.assertEqual(code,0)
        result=self.tickets.store.get(run['id'])
        self.assertEqual(result['node'],'evidence_collection');self.assertEqual(result['status'],'ready')
        self.assertIsNone(self.tickets.get(ticket['id'])['lease_owner'])

    def test_model_capacity_and_unavailable_server_are_explicit_waits(self):
        a=self.add('one');b=self.add('two')
        for run_id in ('one','two'):
            self.tickets.store.checkpoint(run_id,'task_planning',{},'task_planning')
        assigned=claim(self.tickets,2);self.assertEqual(assigned['lease_model'],'a')
        self.assertIsNone(claim(self.tickets,2))
        recover(self.tickets)
        with self.tickets.db:
            self.tickets.db.execute('UPDATE tickets SET not_before=0')
            self.tickets.db.execute('INSERT INTO model_observations VALUES(?,?,?)',('a',0,json.dumps({'available':False})))
        self.assertIsNone(claim(self.tickets,2))
        self.assertIn('unavailable',self.tickets.get(a['id'])['last_reason'])
