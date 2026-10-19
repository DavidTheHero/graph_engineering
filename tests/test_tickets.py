import json
from pathlib import Path
import tempfile
import unittest

from graph_engineering.runtime import Store


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name)
        from graph_engineering.tickets import Tickets
        self.tickets = Tickets(self.home)
        self.store = self.tickets.store

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def ticket(self, key, priority='P2', dependencies=None, **extra):
        run = self.store.create_run({'repo': '/repo/' + key, 'backend': 'local', 'routing': 'baseline', 'models': {}}, key)
        return self.tickets.admit(run['id'], priority=priority, dependencies=dependencies or [], **extra)

    def test_admission_deduplicates_run_and_validates_dependencies(self):
        first = self.ticket('first')
        self.assertEqual(first['id'], self.tickets.admit('first')['id'])
        with self.assertRaises(ValueError): self.ticket('second', dependencies=['missing'])
        self.assertEqual(len(self.tickets.list()), 1)

    def test_dependency_blocks_until_actual_run_completes(self):
        a = self.ticket('first')
        b = self.ticket('second', dependencies=[a['id']])
        snapshot = {t['id']: t for t in self.tickets.list()}
        self.assertEqual(self.tickets.queue(b, snapshot)[0], 'dependency')
        self.store.checkpoint('first', 'completion', {'outcome': 'completed'}, 'done', 'completed')
        snapshot = {t['id']: t for t in self.tickets.list()}
        self.assertEqual(self.tickets.queue(self.tickets.get(b['id']), snapshot)[0], 'normal')

    def test_priority_and_aging_are_deterministic(self):
        from graph_engineering.scheduling_policy import effective_rank, retry_delay
        self.assertLess(effective_rank('P0', 1000, 1000), effective_rank('P3', 1000, 1000))
        self.assertEqual(effective_rank('P3', 0, 1000), 0)
        self.assertEqual(retry_delay(1), 2)
        self.assertEqual(retry_delay(100000), 300)
        with self.assertRaises(ValueError): effective_rank('P0', 0, 0, 0)

    def test_pause_and_retry_are_idempotent_bounded_commands(self):
        t = self.ticket('first')
        a = self.tickets.command(t['id'], 'pause', {}, 'pause-key')
        self.assertEqual(a, self.tickets.command(t['id'], 'pause', {}, 'pause-key'))
        self.assertTrue(self.tickets.get(t['id'])['paused'])
        self.store.checkpoint('first', 'issue_detection', {'error': 'offline'}, 'issue_detection', 'blocked')
        self.tickets.command(t['id'], 'retry', {}, 'retry-one')
        self.tickets.command(t['id'], 'retry', {}, 'retry-one')
        self.assertEqual(self.tickets.get(t['id'])['retries'], 1)
        self.assertEqual(self.store.get('first')['status'], 'ready')
        self.assertGreater(self.tickets.get(t['id'])['not_before'], 0)

    def test_approval_requires_exact_head_and_real_waiting_status(self):
        t = self.ticket('first')
        self.store.checkpoint('first', 'approval_gate', {'head': 'a' * 40}, 'approval_gate', 'waiting_approval')
        with self.assertRaises(ValueError): self.tickets.command(t['id'], 'approve', {'head': 'b' * 40, 'actor': 'human'}, 'bad-head')
        self.tickets.command(t['id'], 'approve', {'head': 'a' * 40, 'actor': 'human'}, 'good-head')
        self.assertEqual(self.store.approved_head('first'), 'a' * 40)


if __name__ == '__main__': unittest.main()
