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
from graph_engineering.runtime import digest
from graph_engineering.workflow import Workflow, adaptable_tests, adapted_test_intact, failing_test_files
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
        with self.assertRaisesRegex(ValueError, 'gained skip'):
            adapted_test_intact(self.repo, 'test_shape.py', sha)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'removed'):
            adapted_test_intact(self.repo, 'test_shape.py', sha)

    def test_only_tests_named_on_failure_lines_count(self):
        """Second review F3: verbose runners name passing files too; a substring is not a path."""
        files = ['tests/a.test.js', 'src/ba.test.js', 'tests/test_a.py', 'tests/conftest.py']
        output = ('PASS tests/a.test.js\ntests/test_a.py ....\n✖ failing tests:\ntest at src/ba.test.js:3:1\n'
                  'FAILED tests/test_a.py::test_x - AssertionError\n')
        self.assertEqual(failing_test_files(output, files), ['src/ba.test.js', 'tests/test_a.py'])
        self.assertEqual(failing_test_files('test at tests/xa.test.js:1:1\n', ['a.test.js']), [])  # Boundary, not substring.

    def test_review_repair_keeps_this_rounds_adapted_tests_and_a_fresh_round_offers_none(self):
        """Second review F4 and F7."""
        flow = type('Flow', (), {'store': self.tickets.store})()
        base = {'intake': {'repository_id': self.registration['id']}, 'repository': {'test_files': ['test_shape.py']},
                'regression': {'path': 'tests/test_graph_x.py'}, 'plan': {'files': ['calc.py', 'test_shape.py'], 'adapted_tests': ['test_shape.py']}}
        with patch('graph_engineering.autonomy.current', return_value={'adapt_existing_tests': True}):
            passed = base | {'verification': {'passed': True, 'regression': {'passed': True}, 'output': 'all green'}}
            self.assertEqual(adaptable_tests(flow, passed), ['test_shape.py'])  # Still adaptable while the review iterates.
            fresh = passed | {'development_round': 1, 'worktree_round': 0,
                              'verification': {'passed': False, 'regression': {'passed': True}, 'output': 'FAIL test_shape.py'}}
            self.assertEqual(adaptable_tests(flow, fresh), [])  # That failure describes code the reset discarded.

    def test_adapted_test_may_not_lose_assertions_focus_or_be_altered_by_the_test_run(self):
        """Second review F5 and F6."""
        sha = git(self.repo, 'rev-parse', 'HEAD').strip()
        path = self.repo / 'test_shape.py'
        original = path.read_text()
        path.write_text(original.replace('self.assertEqual(add(2,3),-1)', 'pass  # covered elsewhere'))
        with self.assertRaisesRegex(ValueError, 'lost assertions'):
            adapted_test_intact(self.repo, 'test_shape.py', sha)
        path.write_text(original.replace('-1)', '5)  # TODO: cover more'))  # A comment is not a todo marker.
        adapted_test_intact(self.repo, 'test_shape.py', sha)
        (self.repo / 'focus.test.js').write_text("test('a', () => { expect(1).toBe(1); });\ntest('b', () => { expect(2).toBe(2); });\n")
        git(self.repo, 'add', 'focus.test.js'); git(self.repo, 'commit', '-q', '-m', 'js test')
        sha2 = git(self.repo, 'rev-parse', 'HEAD').strip()
        (self.repo / 'focus.test.js').write_text("test.only('a', () => { expect(1).toBe(1); });\ntest('b', () => { expect(2).toBe(2); });\n")
        with self.assertRaisesRegex(ValueError, 'focus'):
            adapted_test_intact(self.repo, 'focus.test.js', sha2)
        git(self.repo, 'checkout', '--', 'focus.test.js')  # Only planned files may differ from the base below.
        # F6: the test run may not alter an adapted file; only the applied edits may.
        path.write_text(original.replace('-1)', '5)'))
        state = {'worktree': str(self.repo), 'base_sha': sha2, 'frozen': {'test_shape.py': digest(original.encode().hex())},
                 'plan': {'files': ['calc.py', 'test_shape.py'], 'adapted_tests': ['test_shape.py']},
                 'adapted_digests': {'test_shape.py': digest(path.read_bytes().hex())}, 'regression': {'path': 'tests/test_graph_x.py'}}
        (self.repo / 'calc.py').write_text('def add(a,b): return a+b\n')
        Workflow.verify_files(None, state)  # Edited as applied: fine.
        path.write_text(original.replace('-1)', '5)  # changed by the test process'))
        with self.assertRaisesRegex(ValueError, 'modified outside the applied edits'):
            Workflow.verify_files(None, state)


if __name__ == '__main__':
    unittest.main()
