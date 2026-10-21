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
    """Every implementation edits the previous one and is still wrong: the run escalates."""
    wrong = ['return a-b', 'return a*b', 'return a//b', 'return a%b', 'return a**b', 'return a<<b', 'return a>>b',
             'return a|b', 'return a&b', 'return a^b', 'return -a', 'return b', 'return a']

    def __init__(self):
        super().__init__()
        self.implementations = 0

    def invoke(self, role, *args, **kwargs):
        result = super().invoke(role, *args, **kwargs)
        if role == 'implementation':
            old, new = self.wrong[self.implementations], self.wrong[self.implementations + 1]
            self.implementations += 1
            result['content']['edits'] = [{'path': 'calc.py', 'old': old, 'new': new}]
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
        worker = AlwaysWrong()
        run = self.run_development(worker)
        self.assertEqual((run['node'], run['status']), ('done', 'escalated'))
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

    def test_policy_validation(self):
        with self.assertRaisesRegex(ValueError, 'recovery_rounds'):
            self.set_policy('bad-policy-1', recovery_rounds=7)
        with self.assertRaisesRegex(ValueError, 'requires the development policy'):
            configure(self.tickets, {'repository_id': self.registration['id'], 'actor': 'x', 'development': False, 'recovery_rounds': 1}, 'bad-policy-2')
        self.assertEqual(policy(self.tickets, self.registration['id'])['recovery_rounds'], 0)  # Defaults for older stored policies.


if __name__ == '__main__':
    unittest.main()
