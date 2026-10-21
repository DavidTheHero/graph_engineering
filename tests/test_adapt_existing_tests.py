"""A feature may adapt the existing tests that pin the behaviour it replaces, within bounds (GE-30D05C40, 2026-10-21).

The feature added a ``model`` key to the chat response; ``agent.test.js`` asserted the exact old key list.
Every test file was frozen, so the planner's correct request to adapt it was refused and the ticket stopped."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graph_engineering.attention import item
from graph_engineering.autonomy import configure, dispatch
from graph_engineering.control_repositories import register
from graph_engineering.report_commands import act
from graph_engineering.report_store import create
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import Workflow, adaptable_tests, adapted_test_intact
from tests.test_attention import row
from tests.test_core import git, make_repo
from tests.test_development_handoff import DevelopmentDouble
from tests.test_intake import InvestigationDouble


class Adapting(DevelopmentDouble):
    """Fixes the source first; when the controller offers the pinning test, adapts its assertion."""

    def __init__(self):
        super().__init__()
        self.prompts = []

    def invoke(self, role, prompt, *args, **kwargs):
        result = super().invoke(role, prompt, *args, **kwargs)
        request = json.loads(prompt) if prompt.startswith('{') else {}
        if role in ('planning', 'implementation', 'review'):
            self.prompts.append((role, request))
        if role == 'planning':
            result['content']['files'] = ['calc.py'] + (['test_shape.py'] if request.get('adaptable_existing_tests') else [])
        if role == 'implementation':
            if 'test_shape.py' in request['plan']['files']:
                result['content']['edits'] = [{'path': 'test_shape.py', 'old': 'assertEqual(add(2,3),-1)', 'new': 'assertEqual(add(2,3),5)'}]
            else:
                result['content']['edits'] = [{'path': 'calc.py', 'old': 'return a-b', 'new': 'return a+b'}]
        return result


class AdaptExistingTestsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        # An existing test that pins the behaviour the accepted change replaces.
        (self.repo / 'test_shape.py').write_text('import unittest\nfrom calc import add\nclass Shape(unittest.TestCase):\n def test_pins_old(self): self.assertEqual(add(2,3),-1)\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'Pin the old shape')
        self.home = self.root / 'controller'
        self.tickets = Tickets(self.home)
        self.registration = register(self.tickets, self.repo)
        self.ticket = create(self.tickets, {'repository_id': self.registration['id'], 'description': 'Adding values gives the wrong total'}, 'adapt-create-1')
        flow = Workflow(self.home)
        flow.worker = InvestigationDouble()
        try:
            self.run = flow.execute(self.ticket['run_id'])
        finally:
            flow.store.close()
        self.base = {'ticket_id': self.ticket['ticket_id'], 'revision': 1, 'actor': 'test-operator'}
        act(self.tickets, self.base | {'action': 'accept'}, 'adapt-accept-1')

    def tearDown(self):
        self.tickets.store.close()
        self.temp.cleanup()

    def develop(self, adapt):
        configure(self.tickets, {'repository_id': self.registration['id'], 'actor': 'policy-operator', 'development': True,
                                 'adapt_existing_tests': adapt, 'max_repairs': 2}, 'adapt-policy-' + str(adapt))
        dispatch(self.tickets)
        flow = Workflow(self.home)
        worker = Adapting()
        flow.worker = worker
        try:
            return flow.execute(self.run['id']), worker
        finally:
            flow.store.close()

    def test_pinning_test_is_adapted_under_policy_reviewed_and_shown_at_merge(self):
        run, worker = self.develop(True)
        self.assertEqual(run['status'], 'ready_for_delivery', run['state'].get('error'))
        self.assertEqual(run['state']['plan']['adapted_tests'], ['test_shape.py'])
        plans = [r for role, r in worker.prompts if role == 'planning']
        self.assertNotIn('adaptable_existing_tests', plans[0])  # Not offered until the source change passes the regression.
        self.assertEqual(plans[1]['adaptable_existing_tests'], ['test_shape.py'])
        review = next(r for role, r in worker.prompts if role == 'review')
        self.assertIn('ALSO changes the existing tests test_shape.py', review['task'])
        self.assertIn('existing_tests_planned', [e['kind'] for e in self.tickets.store.events(self.run['id'])])
        self.assertIn('test_shape.py', run['state']['diff'])
        card = item(row('waiting_approval', 'approval_gate', mode='development', head='a' * 40, verification={'passed': True},
                        review={'decision': 'approve'}, plan={'files': ['calc.py', 'test_shape.py'], 'adapted_tests': ['test_shape.py']}))
        self.assertIn('Existing tests adapted to the accepted change: test_shape.py', card['context'])

    def test_without_the_policy_existing_tests_stay_frozen(self):
        run, worker = self.develop(False)
        self.assertNotEqual(run['status'], 'ready_for_delivery')
        self.assertTrue(all('test_shape.py' not in r['files'] for role, r in worker.prompts if role == 'implementation' for r in [r['plan']]))
        self.assertNotIn('existing_tests_planned', [e['kind'] for e in self.tickets.store.events(self.run['id'])])

    def test_only_failing_tests_are_adaptable_and_only_once_the_regression_passes(self):
        flow = type('Flow', (), {'store': self.tickets.store})()
        state = {'intake': {'repository_id': self.registration['id']}, 'repository': {'test_files': ['test_calc.py', 'test_shape.py']},
                 'regression': {'path': 'tests/test_graph_x.py'},
                 'verification': {'passed': False, 'regression': {'passed': True}, 'output': 'FAIL: test_pins_old\n  File "test_shape.py", line 4'}}
        with patch('graph_engineering.autonomy.current', return_value={'adapt_existing_tests': True}):
            self.assertEqual(adaptable_tests(flow, state), ['test_shape.py'])  # Not test_calc.py: it passes.
            self.assertEqual(adaptable_tests(flow, state | {'verification': state['verification'] | {'regression': {'passed': False}}}), [])
            self.assertEqual(adaptable_tests(flow, state | {'verification': state['verification'] | {'passed': True}}), [])
        with patch('graph_engineering.autonomy.current', return_value={'adapt_existing_tests': False}):
            self.assertEqual(adaptable_tests(flow, state), [])

    def test_adapted_test_may_change_assertions_but_never_shrink(self):
        sha = git(self.repo, 'rev-parse', 'HEAD').strip()
        path = self.repo / 'test_shape.py'
        path.write_text(path.read_text().replace('-1)', '5)'))
        adapted_test_intact(self.repo, 'test_shape.py', sha)  # Changed assertion: fine.
        path.write_text('import unittest\nfrom calc import add\nclass Shape(unittest.TestCase):\n pass\n')
        with self.assertRaisesRegex(ValueError, 'lost test definitions'):
            adapted_test_intact(self.repo, 'test_shape.py', sha)
        path.write_text('import unittest\nfrom calc import add\nclass Shape(unittest.TestCase):\n @unittest.skip("x")\n def test_pins_old(self): self.assertEqual(add(2,3),5)\n')
        with self.assertRaisesRegex(ValueError, 'gained skip markers'):
            adapted_test_intact(self.repo, 'test_shape.py', sha)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'removed'):
            adapted_test_intact(self.repo, 'test_shape.py', sha)


if __name__ == '__main__':
    unittest.main()
