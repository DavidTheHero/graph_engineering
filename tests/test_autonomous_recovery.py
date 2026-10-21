"""Policy-owned recovery: stopped developments retry themselves, investigated tickets accept themselves.

GE-30D05C40 (2026-10-21) stopped six times; five were controller or environment faults, and every
one ended with "a human must press Retry". Under the repository policy the controller now does that
itself, bounded by recovery rounds, with merge approval the only remaining human gate."""
import json
import unittest

from graph_engineering.attention import items
from graph_engineering.autonomy import configure, dispatch, policy
from graph_engineering.report_commands import act
from graph_engineering.report_store import create
from graph_engineering.scheduling_policy import execution_limits
from graph_engineering.workflow import Workflow
from tests import test_development_handoff as handoff
from tests.test_development_handoff import DevelopmentDouble
from tests.test_intake import InvestigationDouble


class AlwaysWrong(DevelopmentDouble):
    """Every implementation edits whatever the checkout holds now and is still wrong: the run escalates.

    It reads the current line because a new development round starts from the accepted commit again."""
    wrong = ['return a*b', 'return a//b', 'return a%b', 'return a**b', 'return a<<b', 'return a>>b',
             'return a|b', 'return a&b', 'return a^b', 'return -a', 'return b', 'return a']

    def __init__(self, worktree):
        super().__init__()
        self.worktree = worktree
        self.implementations = 0
        self.seen = []

    def invoke(self, role, *args, **kwargs):
        result = super().invoke(role, *args, **kwargs)
        if role == 'implementation':
            current = next(l.split(': ', 1)[1] for l in (self.worktree / 'calc.py').read_text().splitlines() if 'return' in l)
            self.seen.append(current)
            new = self.wrong[self.implementations % len(self.wrong)]
            self.implementations += 1
            result['content']['edits'] = [{'path': 'calc.py', 'old': current, 'new': new}]
        return result


class AutonomousRecoveryTests(unittest.TestCase):
    setUp = handoff.DevelopmentHandoffTests.setUp
    tearDown = handoff.DevelopmentHandoffTests.tearDown

    def run_development(self, worker):
        flow = Workflow(self.home)
        flow.worker = worker
        try:
            return flow.execute(self.run['id'])
        finally:
            flow.store.close()

    def set_policy(self, key, **fields):
        data = {'repository_id': self.registration['id'], 'actor': 'policy-operator', 'development': True} | fields
        return configure(self.tickets, data, key)

    def test_escalated_development_recovers_itself_for_the_policy_rounds_then_asks(self):
        act(self.tickets, self.base | {'action': 'start_development', 'trust_execution': True}, 'recover-start-1')
        worker = AlwaysWrong(self.home / 'worktrees' / self.run['id'] / 'implementation')
        run = self.run_development(worker)
        self.assertEqual((run['node'], run['status']), ('done', 'escalated'))
        self.assertEqual(worker.seen[0], 'return a-b')
        self.tickets.db.execute('UPDATE tickets SET max_retries=1 WHERE id=?', (self.ticket['ticket_id'],))
        self.tickets.db.commit()
        self.set_policy('recover-policy-1', recovery_rounds=2)
        self.assertEqual(items(self.tickets), [])  # The policy will act: nobody needs to.
        dispatch(self.tickets)
        state = self.tickets.store.get(self.run['id'])
        self.assertEqual((state['node'], state['status'], state['state']['autonomy_recoveries']), ('task_planning', 'ready', 1))
        self.tickets.db.execute('UPDATE tickets SET not_before=0 WHERE id=?', (self.ticket['ticket_id'],))
        self.tickets.db.commit()
        run = self.run_development(worker)
        self.assertEqual(run['status'], 'escalated', (run['node'], run['state'].get('error')))
        self.assertEqual(worker.seen[3], 'return a-b')  # Round 2 started from the accepted commit, not from round 1's edits.
        ticket = self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((ticket['retries'], ticket['max_retries']), (1, 1))  # Exhausted: the round extends it by one.
        dispatch(self.tickets)
        ticket = self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((ticket['retries'], ticket['max_retries'], ticket['state']['autonomy_recoveries']), (2, 2, 2))
        kinds = [e['kind'] for e in self.tickets.store.events(self.run['id'])]
        self.assertEqual((kinds.count('autonomy_recovery'), kinds.count('retry_budget_extended')), (2, 1))
        self.tickets.db.execute('UPDATE tickets SET not_before=0 WHERE id=?', (self.ticket['ticket_id'],))
        self.tickets.db.commit()
        run = self.run_development(worker)
        self.assertEqual(run['status'], 'escalated', (run['node'], run['state'].get('error')))
        dispatch(self.tickets)  # Rounds used: now a person decides.
        self.assertEqual(self.tickets.get(self.ticket['ticket_id'])['state']['autonomy_recoveries'], 2)
        [found] = items(self.tickets)
        self.assertEqual((found['ticket_id'], found['kind']), (self.ticket['ticket_id'], 'dead_letter'))

    def test_unrecoverable_stops_are_not_retried(self):
        act(self.tickets, self.base | {'action': 'start_development', 'trust_execution': True}, 'unrecoverable-start-1')
        self.tickets.store.db.execute("UPDATE runs SET node='regression_review',status='blocked',state=json_set(state,'$.error',?) WHERE id=?",
                                      ('Regression review rejected twice; refine the accepted requirement', self.run['id']))
        self.tickets.store.db.commit()
        self.set_policy('unrecoverable-policy-1', recovery_rounds=3)
        dispatch(self.tickets)
        ticket = self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((ticket['status'], ticket['retries'], ticket['state'].get('autonomy_recoveries', 0)), ('blocked', 0, 0))
        self.assertEqual([i['ticket_id'] for i in items(self.tickets)], [self.ticket['ticket_id']])  # Still needs a person.

    def test_policy_budgets_apply_to_autonomous_development(self):
        self.set_policy('budget-policy-1', max_repairs=3, max_node_attempts=3)
        dispatch(self.tickets)  # Accepted ticket: the policy starts development itself.
        run = self.run_development(DevelopmentDouble())
        self.assertEqual(run['status'], 'ready_for_delivery', run['state'].get('error'))
        self.assertEqual((run['state']['config']['max_repairs'], run['state']['config']['max_node_attempts']), (3, 3))
        self.assertEqual(execution_limits(self.tickets.get(self.ticket['ticket_id']))['step_limit'], 3)

    def test_investigated_ticket_is_accepted_automatically_only_when_clean(self):
        second = create(self.tickets, {'repository_id': self.registration['id'], 'description': 'The multiply helper ignores its second argument entirely'}, 'auto-accept-create-1')
        flow = Workflow(self.home)
        flow.worker = InvestigationDouble()
        try:
            self.assertEqual(flow.execute(second['run_id'])['status'], 'ready_for_review')
        finally:
            flow.store.close()
        configure(self.tickets, {'repository_id': self.registration['id'], 'actor': 'policy-operator', 'auto_accept': True}, 'auto-accept-policy-1')
        self.assertNotIn(second['ticket_id'], [i['ticket_id'] for i in items(self.tickets)])
        dispatch(self.tickets)
        accepted = self.tickets.get(second['ticket_id'])
        self.assertEqual((accepted['status'], accepted['state']['accepted_revision']), ('ready_for_development', accepted['state']['revision']))
        # With an open question the policy must not accept, whatever the references say.
        self.tickets.store.db.execute("UPDATE runs SET status='ready_for_review',node='ticket_finalisation',state=json_set(state,'$.accepted_revision',null,'$.enhanced.questions',json('[\"Which theme?\"]')) WHERE id=?", (second['run_id'],))
        self.tickets.store.db.commit()
        dispatch(self.tickets)
        self.assertEqual(self.tickets.get(second['ticket_id'])['status'], 'ready_for_review')

    def test_paused_stop_stays_visible_and_is_not_recovered(self):
        """Review of PR #8: dispatch never touches a paused ticket, so hiding it would lose it."""
        act(self.tickets, self.base | {'action': 'start_development', 'trust_execution': True}, 'paused-start-1')
        self.tickets.store.db.execute("UPDATE runs SET status='blocked',state=json_set(state,'$.error','stalled') WHERE id=?", (self.run['id'],))
        self.tickets.store.db.commit()
        self.tickets.command(self.ticket['ticket_id'], 'pause', {}, 'paused-pause-1')
        self.set_policy('paused-policy-1', recovery_rounds=3)
        self.assertEqual([i['ticket_id'] for i in items(self.tickets)], [self.ticket['ticket_id']])
        dispatch(self.tickets)
        ticket = self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((ticket['status'], ticket['retries'], ticket['state'].get('autonomy_recoveries', 0)), ('blocked', 0, 0))
        # A paused scheduler sweeps nothing either: the operator must still see the stop.
        self.tickets.command(self.ticket['ticket_id'], 'resume', {}, 'paused-resume-1')
        self.tickets.set_setting('paused', True)
        self.assertEqual([i['ticket_id'] for i in items(self.tickets)], [self.ticket['ticket_id']])

    def test_revised_requirement_is_accepted_again_with_fresh_recovery_rounds(self):
        """Review of PR #8: the second acceptance cycle must not reuse the first cycle's idempotency keys."""
        self.set_policy('revise-policy-1', auto_accept=True, recovery_rounds=2)
        dispatch(self.tickets)  # Accepted in setUp; the policy starts development.
        self.assertEqual(self.tickets.get(self.ticket['ticket_id'])['node'], 'development_setup')
        self.tickets.store.db.execute("UPDATE runs SET status='blocked',state=json_set(state,'$.error','stalled','$.autonomy_recoveries',2) WHERE id=?", (self.run['id'],))
        self.tickets.store.db.commit()
        act(self.tickets, self.base | {'action': 'revise_requirement'}, 'revise-1')
        ticket = self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((ticket['status'], ticket['state'].get('autonomy_recoveries', 0), ticket['state']['acceptance_cycle']), ('ready_for_review', 0, 1))
        dispatch(self.tickets)  # Accepted again...
        dispatch(self.tickets)  # ...and started again, with the same revision number.
        ticket = self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((ticket['status'], ticket['node']), ('ready', 'development_setup'))
        self.assertEqual(ticket['last_reason'], '')  # Nothing deferred silently.

    def test_whole_run_execution_honours_the_policy_attempt_budget(self):
        """Review of PR #8: a single execute() must see the budget development_setup applied, not the run's old one."""
        class PassingRegression(DevelopmentDouble):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == 'regression_test':
                    result['content']['content'] = 'import unittest\nfrom calc import add\nclass R(unittest.TestCase):\n def test_x(self): self.assertEqual(add(9,6),3)\n'
                return result
        self.set_policy('whole-run-policy-1', max_node_attempts=3)
        dispatch(self.tickets)
        worker = PassingRegression()
        run = self.run_development(worker)
        self.assertEqual(run['status'], 'blocked')
        self.assertTrue(str(run['state'].get('error', {}).get('message', '')).startswith('Regression repair budget exhausted'), run['state'].get('error'))
        self.assertEqual(sum(m.get('role') == 'regression_test' for m in worker.metrics), 3)  # Three proposals, not two.

    def test_budget_raised_while_a_run_is_underway_applies_at_the_next_step(self):
        """GE-30D05C40: the policy said 3 attempts, the run kept the 2 its setup had copied and blocked one attempt early."""
        act(self.tickets, self.base | {'action': 'start_development', 'trust_execution': True}, 'raise-start-1')
        flow = Workflow(self.home); flow.worker = DevelopmentDouble()
        try:
            run = flow.execute(self.run['id'], until='task_planning')
            self.assertEqual(run['state']['config']['max_node_attempts'], 2)
            self.set_policy('raise-policy-1', max_node_attempts=4, max_repairs=3)
            run = flow.execute(self.run['id'], until='isolated_implementation')
        finally:
            flow.store.close()
        self.assertEqual((run['state']['config']['max_node_attempts'], run['state']['config']['max_repairs']), (4, 3))
        self.assertIn('policy_budget_applied', [e['kind'] for e in self.tickets.store.events(self.run['id'])])

    def test_sweep_skips_new_tickets_and_survives_a_failing_one(self):
        """GE-FDD2275A: a freshly filed ticket has no revision; reading it raised KeyError and killed the worker service."""
        from unittest.mock import patch
        import graph_engineering.autonomy as autonomy
        self.set_policy('sweep-policy-1', auto_accept=True, recovery_rounds=2)
        fresh = create(self.tickets, {'repository_id': self.registration['id'], 'description': 'A brand new report not investigated yet'}, 'sweep-create-1')
        dispatch(self.tickets)  # Must not raise; the accepted ticket from setUp is still started.
        self.assertEqual(self.tickets.get(self.ticket['ticket_id'])['node'], 'development_setup')
        self.assertEqual(self.tickets.get(fresh['ticket_id'])['node'], 'manual_intake')
        # Review of PR #17: an unexpected error for one ticket is recorded once, never as its block reason, and the
        # sweep still reaches the tickets after it; the connection is left without an open transaction.
        before = self.tickets.get(self.ticket['ticket_id'])['last_reason']
        real = autonomy.acceptable
        seen = []
        def flaky(ticket):
            seen.append(ticket['id'])
            if ticket['id'] == self.ticket['ticket_id']: raise RuntimeError('boom')
            return real(ticket)
        with patch('graph_engineering.autonomy.acceptable', side_effect=flaky), \
             patch.object(self.tickets, 'list', return_value=[self.tickets.get(self.ticket['ticket_id']), self.tickets.get(self.ticket['ticket_id']) | {'id': 'GE-OTHER'}]):
            dispatch(self.tickets); dispatch(self.tickets)
        self.assertEqual(seen.count('GE-OTHER'), 2)  # The ticket after the failing one is still considered.
        self.assertEqual(self.tickets.get(self.ticket['ticket_id'])['last_reason'], before)
        errors = [e['data']['error'] for e in self.tickets.store.events(self.run['id']) if e['kind'] == 'autonomy_sweep_error']
        self.assertEqual(errors, ['RuntimeError: boom'])  # Once per distinct error.
        self.assertFalse(self.tickets.db.in_transaction)

    def test_failed_recovery_retry_gives_the_round_back(self):
        from unittest.mock import patch
        from graph_engineering.autonomy import recover
        act(self.tickets, self.base | {'action': 'start_development', 'trust_execution': True}, 'revert-start-1')
        self.tickets.store.db.execute("UPDATE runs SET status='blocked',state=json_set(state,'$.error','stalled') WHERE id=?", (self.run['id'],))
        self.tickets.db.execute('UPDATE tickets SET max_retries=0 WHERE id=?', (self.ticket['ticket_id'],)); self.tickets.db.commit()
        ticket = self.tickets.get(self.ticket['ticket_id'])
        setting = policy(self.tickets, self.registration['id']) | {'recovery_rounds': 2, 'actor': 'x'}
        with patch.object(self.tickets, 'command', side_effect=ValueError('lease raced')):
            with self.assertRaises(ValueError):
                recover(self.tickets, ticket, setting)
        after = self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((after['max_retries'], after['state'].get('autonomy_recoveries', 0)), (0, 0))
        self.assertIn('autonomy_recovery_reverted', [e['kind'] for e in self.tickets.store.events(self.run['id'])])

    def test_scheduler_keeps_claiming_when_the_sweep_fails(self):
        from unittest.mock import patch
        from graph_engineering.scheduler import serve_scheduler
        with patch('graph_engineering.autonomy.dispatch', side_effect=KeyError('revision')), \
             patch('graph_engineering.scheduler.claim', return_value=None) as claim, patch('traceback.print_exc') as printed:
            serve_scheduler(self.home, workers=1, once=True)
        self.assertTrue(claim.called and printed.called)
        self.assertEqual(self.tickets.setting('autonomy_sweep_error')['error'], "KeyError('revision')")

    def test_policy_validation(self):
        with self.assertRaisesRegex(ValueError, 'recovery_rounds'):
            self.set_policy('bad-policy-1', recovery_rounds=7)
        with self.assertRaisesRegex(ValueError, 'requires the development policy'):
            configure(self.tickets, {'repository_id': self.registration['id'], 'actor': 'x', 'development': False, 'recovery_rounds': 1}, 'bad-policy-2')
        self.assertEqual(policy(self.tickets, self.registration['id'])['recovery_rounds'], 0)  # Defaults for older stored policies.


if __name__ == '__main__':
    unittest.main()
