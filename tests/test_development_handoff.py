"""Accepted-ticket execution uses real Git/tests and labelled inference doubles."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graph_engineering.report_commands import act
from graph_engineering.report_store import create
from graph_engineering.scheduling_policy import board_status
from graph_engineering.tickets import Tickets
from graph_engineering.control_repositories import register
from graph_engineering.workflow import Workflow
from tests.test_core import make_repo,git
from tests.test_intake import InvestigationDouble
from tests.test_workflow import WorkerDouble


class DevelopmentDouble(WorkerDouble):
    def invoke(self,role,*args,**kwargs):
        if role=='question_answering':
            content={'answers':[{'question':'Which inputs reproduce the failure?','answer':'Use the operator-provided values 9 and 6.',
                'status':'answered_from_operator','basis':['Troubleshooting note: Use real input values 9 and 6.']}],
                'summary':'One question was answered from recorded operator information.'}
        elif role=='regression_test':
            content={'content':'import unittest\nfrom calc import add\nclass Regression(unittest.TestCase):\n def test_accepted_requirement(self): self.assertEqual(add(9,6),15)\n','rationale':'Direct accepted addition criterion'}
        elif role=='implementation':
            content={'summary':'Restore addition','edits':[{'path':'calc.py','old':'return a-b','new':'return a+b'}]}
        else:return super().invoke(role,*args,**kwargs)
        metric={'role':role,'success':True,'model_key':args[2] if len(args)>2 else 'a','model_id':'explicit-development-double','latency_seconds':0}
        self.metrics.append(metric);return {'content':content,'metrics':metric,'raw_path':'explicit-development-double'}


class DevelopmentHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.home=self.root/'controller'
        self.repo=make_repo(self.root);self.tickets=Tickets(self.home);self.registration=register(self.tickets,self.repo)
        self.ticket=create(self.tickets,{'repository_id':self.registration['id'],'description':'Adding values gives the wrong total'},'handoff-create-1')
        flow=Workflow(self.home);flow.worker=InvestigationDouble()
        try:self.run=flow.execute(self.ticket['run_id'])
        finally:flow.store.close()
        self.base={'ticket_id':self.ticket['ticket_id'],'revision':1,'actor':'test-operator'}
        act(self.tickets,self.base|{'action':'accept'},'handoff-accept-1')

    def tearDown(self):self.tickets.store.close();self.temp.cleanup()

    def test_cancel_has_distinct_persistent_column_and_cannot_be_accidentally_resumed(self):
        self.tickets.command(self.ticket['ticket_id'],'cancel',{},'handoff-cancel-1')
        ticket=self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual(board_status(ticket['node'],ticket['status'],True),'Cancelled')
        self.assertEqual(self.tickets.queue(ticket)[0],'cancelled')
        with self.assertRaises(ValueError):act(self.tickets,self.base|{'action':'send_development'},'cancelled-admit-1')
        with self.assertRaises(ValueError):self.tickets.command(ticket['id'],'resume',{},'cancelled-resume-1')

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_explicit_start_executes_accepted_ticket_regression_fix_and_review(self,_):
        with self.assertRaises(ValueError):act(self.tickets,self.base|{'action':'start_development'},'handoff-denied-1')
        request=self.base|{'action':'start_development','trust_execution':True}
        result=act(self.tickets,request,'handoff-start-1')
        self.assertEqual(result,act(self.tickets,request,'handoff-start-1'))
        self.assertEqual(self.tickets.get(self.ticket['ticket_id'])['node'],'development_setup')
        flow=Workflow(self.home);flow.worker=DevelopmentDouble()
        try:
            run=flow.execute(self.run['id']);self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
            self.assertFalse(run['state']['regression_baseline']['passed'])
            self.assertTrue(run['state']['verification']['regression']['passed'])
            self.assertTrue(run['state']['review']['approved'])
            self.assertIn('test_graph_',run['state']['diff']);self.assertNotIn('pr',run['state'])
            self.assertEqual(git(self.repo,'rev-parse','HEAD'),self.run['state']['base_sha'])
            self.assertFalse(git(self.repo,'status','--porcelain'))
            act(self.tickets,self.base|{'action':'publish_development','approve_publication':True},'handoff-deliver-1')
            published=flow.execute(self.run['id']);self.assertEqual(published['status'],'waiting_approval',published['state'].get('error'))
        finally:flow.store.close()

    def test_development_cannot_start_without_current_acceptance_or_after_cancellation(self):
        act(self.tickets,self.base|{'action':'edit_enhancement','changes':{'title':'Updated addition task'}},'handoff-edit-1')
        with self.assertRaises(ValueError):act(self.tickets,self.base|{'revision':2,'action':'start_development','trust_execution':True},'unaccepted-start-1')
        self.assertEqual(git(self.repo,'status','--porcelain'),'')

    def test_blocked_development_accepts_context_without_resetting_scope_or_limits(self):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'context-start-1')
        self.tickets.store.checkpoint(self.run['id'],'regression_authoring',{'attempts':{'regression_authoring:0':2},'error':'Attempt budget exhausted'},'regression_authoring','blocked')
        self.tickets.command(self.ticket['ticket_id'],'pause',{},'context-pause-1')
        request=self.base|{'action':'development_information','text':'Deletion must verify dependent records and actual permission denial.'}
        first=act(self.tickets,request,'context-save-1');self.assertEqual(first,act(self.tickets,request,'context-save-1'))
        ticket=self.tickets.get(self.ticket['ticket_id']);state=ticket['state']
        self.assertEqual(ticket['status'],'blocked');self.assertEqual(state['accepted_revision'],1)
        self.assertEqual(state['attempts']['regression_authoring:0'],2);self.assertEqual(ticket['retries'],0)
        self.assertTrue(ticket['paused'])
        self.assertEqual(len(state['development_information']),1)
        self.assertEqual(state['development_information'][0]['text'],request['text'])
        with self.assertRaises(ValueError):act(self.tickets,self.base|{'action':'edit_enhancement','changes':{'title':'Different scope'}},'context-change-scope')

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_qwen_4b_answers_blocked_questions_without_spending_a_retry(self,_):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'questions-start-1')
        state=self.tickets.get(self.ticket['ticket_id'])['state']
        state['config']['routing']='multimodel'
        state['enhanced']['questions']=['Which inputs reproduce the failure?']
        state['development_information']=[{'text':'Use real input values 9 and 6.','actor':'test-operator','at':1}]
        self.tickets.store.checkpoint(self.run['id'],'regression_review',
            {'attempts':{'regression_review:0':2},'error':'Attempt budget exhausted',**{k:v for k,v in state.items() if k in ('config','enhanced','development_information')}},
            'regression_review','blocked')
        request=self.base|{'action':'answer_questions'}
        first=act(self.tickets,request,'questions-answer-1')
        self.assertEqual(first,act(self.tickets,request,'questions-answer-1'))
        queued=self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((queued['node'],queued['status']),('question_answering','ready'))
        flow=Workflow(self.home);worker=DevelopmentDouble();flow.worker=worker
        try:
            answered=flow.execute(self.run['id'])
            self.assertEqual((answered['node'],answered['status']),('regression_review','blocked'))
            self.assertEqual(answered['state']['error'],'Attempt budget exhausted')
            self.assertEqual(answered['state']['accepted_revision'],1)
            self.assertEqual(self.tickets.get(self.ticket['ticket_id'])['retries'],0)
            self.assertEqual(answered['state']['question_answers']['answers'][0]['status'],'answered_from_operator')
            call=next(metric for metric in worker.metrics if metric.get('role')=='question_answering')
            self.assertEqual(call['model_key'],'b')
        finally:flow.store.close()

    def test_exhaustion_projection_explains_both_retry_limits(self):
        from graph_engineering.scheduling_policy import execution_limits
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'limits-start-1')
        self.tickets.store.checkpoint(self.run['id'],'regression_authoring',{'attempts':{'regression_authoring:0':2},'error':'Attempt budget exhausted',
            'regression_review':{'approved':False,'findings':[{'severity':'critical','message':'Permission assertion is commented out.'}]}},'regression_authoring','blocked')
        ticket=self.tickets.get(self.ticket['ticket_id']);limits=execution_limits(ticket)
        self.assertEqual((limits['step_used'],limits['step_limit'],limits['retries_used'],limits['retry_limit']),(2,2,0,3))
        reason=self.tickets.queue(ticket)[1]
        self.assertIn('2/2',reason);self.assertIn('0/3',reason);self.assertIn('Permission assertion is commented out',reason)

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_saved_context_reaches_next_worker_after_explicit_retry(self,_):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'worker-context-start')
        self.tickets.store.checkpoint(self.run['id'],'development_setup',{'error':'Attempt budget exhausted'},'development_setup','blocked')
        act(self.tickets,self.base|{'action':'development_information','text':'Use real input values 9 and 6.'},'worker-context-note')
        self.tickets.command(self.ticket['ticket_id'],'retry',{},'worker-context-retry')
        class ContextDouble(DevelopmentDouble):
            saw_context=False
            def invoke(self,role,*args,**kwargs):
                if role=='regression_test':self.saw_context='Use real input values 9 and 6.' in args[0]
                return super().invoke(role,*args,**kwargs)
        flow=Workflow(self.home);worker=ContextDouble();flow.worker=worker
        try:
            run=flow.execute(self.run['id']);self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
            self.assertTrue(worker.saw_context)
        finally:flow.store.close()

    def test_review_rejection_retry_returns_to_authoring_with_a_fresh_review_loop(self):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'review-retry-start')
        review={'approved':False,'findings':[{'severity':'critical','message':'Test bypasses the public deletion API.'}]}
        self.tickets.store.checkpoint(self.run['id'],'development_setup',
            {'regression_review':review,'regression_review_round':2,'error':'Regression review rejected twice; refine the accepted requirement'},
            'regression_review','blocked')
        self.tickets.command(self.ticket['ticket_id'],'retry',{},'review-retry-command')
        ticket=self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual(ticket['node'],'regression_authoring')
        self.assertEqual(ticket['status'],'ready')
        self.assertEqual(ticket['retries'],1)
        self.assertEqual(ticket['state']['regression_review_round'],0)
        self.assertEqual(ticket['state']['regression_review'],review)

    def test_passing_or_invalid_regression_blocks_implementation(self):
        class PassingRegression(DevelopmentDouble):
            def invoke(self,role,*args,**kwargs):
                result=super().invoke(role,*args,**kwargs)
                if role=='regression_test':result['content']['content']='import unittest\nfrom calc import add\nclass Regression(unittest.TestCase):\n def test_observation(self): self.assertEqual(add(9,6),3)\n'
                return result
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'passing-start-1')
        flow=Workflow(self.home);flow.worker=PassingRegression()
        try:
            run=flow.execute(self.run['id']);self.assertEqual(run['status'],'blocked')
            self.assertEqual(run['node'],'regression_validation');self.assertNotIn('worktree',run['state'])
        finally:flow.store.close()

    def test_fragment_edits_reject_ambiguous_matches_and_outside_paths(self):
        from graph_engineering.accepted_development import materialize_hunks
        for edits in ([{'path':'../owned.py','old':'a','new':'b'}],[{'path':'calc.py','old':'a','new':'b'}]):
            with self.assertRaises(ValueError):materialize_hunks(self.repo,{'edits':edits},['calc.py'])

    def test_partial_dependency_directory_does_not_count_as_success(self):
        from graph_engineering.accepted_development import prepare_dependencies
        from types import SimpleNamespace
        path=self.home/'worktrees'/'dependency-test';path.mkdir(parents=True)
        (path/'package-lock.json').write_text('{}');(path/'node_modules').mkdir()
        state={'config':{'command_timeout':10}}
        failed={'returncode':1,'timed_out':False};passed={'returncode':0,'timed_out':False}
        flow=SimpleNamespace(root=self.home,run_id=self.run['id'],cancelled=lambda:False,store=self.tickets.store)
        with patch('graph_engineering.autonomy.current',return_value={'prepare_dependencies':True}),patch('graph_engineering.accepted_development.command',side_effect=[failed,passed]) as execute:
            with self.assertRaises(ValueError):prepare_dependencies(flow,state,path)
            prepare_dependencies(flow,state,path);prepare_dependencies(flow,state,path)
            self.assertEqual(execute.call_count,2)

    def test_repository_policy_dispatches_accepted_work_once_and_preserves_cancellation(self):
        from graph_engineering.autonomy import configure,dispatch,policy
        self.assertFalse(policy(self.tickets,self.registration['id'])['development'])
        setting={'repository_id':self.registration['id'],'actor':'test-operator','development':True,'publish':True}
        configure(self.tickets,setting,'autonomy-enable-1');dispatch(self.tickets);dispatch(self.tickets)
        ticket=self.tickets.get(self.ticket['ticket_id']);self.assertEqual(ticket['node'],'development_setup')
        self.assertFalse(ticket['state']['config']['auto_merge'])
        self.assertEqual(sum(e['kind']=='intake_start_development' for e in self.tickets.store.events(ticket['run_id'])),1)
        self.tickets.command(ticket['id'],'cancel',{},'autonomy-cancel-1');dispatch(self.tickets)
        self.assertEqual(self.tickets.get(ticket['id'])['status'],'cancelled')

    def test_revoked_policy_and_concurrent_admission_do_not_start_or_crash(self):
        from graph_engineering.autonomy import configure,dispatch
        data={'repository_id':self.registration['id'],'actor':'test-operator','development':True}
        enabled=configure(self.tickets,data,'autonomy-race-enabled')
        configure(self.tickets,data|{'development':False},'autonomy-race-revoked')
        with self.assertRaises(ValueError):
            act(self.tickets,self.base|{'action':'start_development','trust_execution':True,'autonomy_policy_updated':enabled['updated']},'stale-policy-start')
        configure(self.tickets,data,'autonomy-race-reenabled')
        with patch('graph_engineering.report_commands.act',side_effect=ValueError('Ticket revision changed; reload before acting')):
            dispatch(self.tickets);dispatch(self.tickets)
        self.assertEqual(sum(e['kind']=='autonomy_admission_deferred' for e in self.tickets.store.events(self.run['id'])),1)
        self.assertEqual(self.tickets.get(self.ticket['ticket_id'])['node'],'development_admission')

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_interrupted_fragment_application_reuses_persisted_proposal(self,_):
        from graph_engineering.repository import Worktree
        apply=Worktree.apply;calls=[]
        def interrupted(worktree,edits):
            apply(worktree,edits);calls.append(1)
            if len(calls)==1:raise RuntimeError('Injected controller interruption after writing source')
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'interrupted-start-1')
        flow=Workflow(self.home);worker=DevelopmentDouble();flow.worker=worker
        try:
            with patch.object(Worktree,'apply',interrupted):run=flow.execute(self.run['id'])
            self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
            self.assertEqual(len(calls),2)
            self.assertEqual(sum(m.get('role')=='implementation' for m in worker.metrics),1)
        finally:flow.store.close()

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_redacted_cached_source_is_never_reapplied_as_code(self,_):
        import os
        from graph_engineering.repository import Worktree
        apply=Worktree.apply
        def interrupted(worktree,edits):
            apply(worktree,edits);raise RuntimeError('Injected interruption after application')
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'redacted-recovery-start')
        flow=Workflow(self.home);flow.worker=DevelopmentDouble()
        try:
            with patch.dict(os.environ,{'GRAPH_TEST_SECRET':'def add(a,b): return a+b'}),patch.object(Worktree,'apply',interrupted):
                run=flow.execute(self.run['id'])
            self.assertEqual(run['status'],'blocked')
            self.assertIn('Persisted source proposal changed',run['state']['error']['message'])
            content=(self.home/'worktrees'/self.run['id']/'implementation'/'calc.py').read_text()
            self.assertEqual(content,'def add(a,b): return a+b\n');self.assertNotIn('pr',run['state'])
        finally:flow.store.close()

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_policy_runs_through_local_pr_and_stops_at_merge_approval(self,_):
        from graph_engineering.autonomy import configure,dispatch
        configure(self.tickets,{'repository_id':self.registration['id'],'actor':'test-operator','development':True,'publish':True},'autonomy-through-1')
        dispatch(self.tickets);flow=Workflow(self.home);flow.worker=DevelopmentDouble()
        try:
            run=flow.execute(self.run['id']);self.assertEqual(run['status'],'waiting_approval',run['state'].get('error'))
            self.assertIn('pr',run['state']);self.assertNotIn('merge',run['state'])
        finally:flow.store.close()

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_empty_generated_test_is_repaired_without_execution_or_operator_input(self,_):
        class MetaDouble(DevelopmentDouble):
            calls=0
            def invoke(self,role,*args,**kwargs):
                result=super().invoke(role,*args,**kwargs)
                if role=='regression_test':
                    self.calls+=1
                    if self.calls==1:result['content']['content']='import unittest # I should write a real test later\n'
                return result
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'meta-start-1')
        flow=Workflow(self.home);worker=MetaDouble();flow.worker=worker
        try:
            run=flow.execute(self.run['id']);self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
            self.assertEqual(worker.calls,2)
        finally:flow.store.close()
