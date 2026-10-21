"""Accepted-ticket execution uses real Git/tests and labelled inference doubles."""
import copy
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

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_reasking_after_question_answering_blocked_returns_to_original_checkpoint(self,_):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'reask-start-1')
        state=self.tickets.get(self.ticket['ticket_id'])['state']
        state['config']['routing']='multimodel'
        state['enhanced']['questions']=['Which inputs reproduce the failure?']
        state['development_information']=[{'text':'Use real input values 9 and 6.','actor':'test-operator','at':1}]
        self.tickets.store.checkpoint(self.run['id'],'regression_review',
            {'attempts':{'regression_review:0':2},'error':'Attempt budget exhausted',**{k:v for k,v in state.items() if k in ('config','enhanced','development_information')}},
            'regression_review','blocked')
        act(self.tickets,self.base|{'action':'answer_questions'},'reask-answer-1')
        # The question-answering worker itself fails and blocks at its own node.
        self.tickets.store.checkpoint(self.run['id'],'question_answering',{'error':{'type':'KeyError','message':"'question_answering'"}},
            'question_answering','blocked')
        act(self.tickets,self.base|{'action':'answer_questions'},'reask-answer-2')
        request=self.tickets.get(self.ticket['ticket_id'])['state']['question_answer_request']
        self.assertEqual((request['return_node'],request['return_error']),('regression_review','Attempt budget exhausted'))
        flow=Workflow(self.home);flow.worker=DevelopmentDouble()
        try:
            answered=flow.execute(self.run['id'])
            self.assertEqual((answered['node'],answered['status']),('regression_review','blocked'))
            self.assertEqual(answered['state']['error'],'Attempt budget exhausted')
        finally:flow.store.close()

    def test_reasking_without_a_recorded_checkpoint_is_refused(self):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'lost-start-1')
        state=self.tickets.get(self.ticket['ticket_id'])['state']
        self.tickets.store.checkpoint(self.run['id'],'question_answering',
            {'enhanced':state['enhanced']|{'questions':['Which inputs reproduce the failure?']},
             'question_answer_request':{'return_node':'question_answering','mode':'blocked'}},'question_answering','blocked')
        with self.assertRaisesRegex(ValueError,'checkpoint to return to'):
            act(self.tickets,self.base|{'action':'answer_questions'},'lost-answer-1')

    def test_revise_requirement_returns_exhausted_ticket_to_review_and_restarts_cleanly(self):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'revise-start-1')
        self.tickets.store.checkpoint(self.run['id'],'regression_review',
            {'regression_review':{'approved':False,'findings':[]},'regression_review_round':2,
             'error':'Regression review rejected twice; refine the accepted requirement'},'regression_review','blocked')
        act(self.tickets,self.base|{'action':'development_information','text':'Drop the role feature.'},'revise-note-1')
        self.tickets.db.execute("UPDATE tickets SET retries=3 WHERE id=?",(self.ticket['ticket_id'],));self.tickets.db.commit()
        with self.assertRaises(ValueError):self.tickets.command(self.ticket['ticket_id'],'retry',{},'revise-retry-1')
        result=act(self.tickets,self.base|{'action':'revise_requirement'},'revise-requirement-1')
        self.assertEqual(result['status'],'ready_for_review')
        ticket=self.tickets.get(self.ticket['ticket_id']);state=ticket['state']
        self.assertEqual((ticket['node'],ticket['retries'],state['config']['run_mode']),('ticket_finalisation',0,'intake'))
        self.assertEqual(state['config']['repo'],state['config']['source_repo'])
        self.assertIsNone(state['accepted_revision'])
        for field in ('development','development_information','regression_review','regression_review_round','error'):
            self.assertNotIn(field,state)
        self.assertEqual(state['clarifications'][-1]['text'],'Drop the role feature.')
        # The operator edits the criteria, clears the answered questions, accepts and authorises development again.
        act(self.tickets,self.base|{'action':'edit_enhancement','changes':{'acceptance_criteria':['Adding 9 and 6 returns 15.'],'questions':[]}},'revise-edit-1')
        revised=self.base|{'revision':2}
        act(self.tickets,revised|{'action':'accept'},'revise-accept-1')
        act(self.tickets,revised|{'action':'start_development','trust_execution':True},'revise-restart-1')
        ticket=self.tickets.get(self.ticket['ticket_id'])
        self.assertEqual((ticket['node'],ticket['status']),('development_setup','ready'))
        self.assertEqual(ticket['state']['enhanced']['questions'],[])
        self.assertEqual(ticket['state']['development']['authorised_revision'],2)

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_setup_after_an_earlier_attempt_starts_from_clean_checkouts_and_current_timeout(self,_):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'clean-setup-start')
        flow=Workflow(self.home);flow.worker=DevelopmentDouble()
        try:first=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual(first['status'],'ready_for_delivery',first['state'].get('error'))
        baseline=Path(first['state']['baseline_path'])
        self.assertTrue((baseline/first['state']['regression']['path']).is_file())  # Installed by regression validation.
        config_path=self.tickets.db.execute('SELECT config_path FROM registered_repositories WHERE id=?',(self.registration['id'],)).fetchone()[0]
        config=json.loads(Path(config_path).read_text(encoding='utf-8-sig'));config['command_timeout']=300
        Path(config_path).write_text(json.dumps(config),encoding='utf-8')
        # As after Revise requirement: development restarts at setup on the same run.
        self.tickets.store.checkpoint(self.run['id'],'development_setup',{},'development_setup','ready')
        flow=Workflow(self.home);flow.worker=DevelopmentDouble()
        try:second=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual(second['status'],'ready_for_delivery',second['state'].get('error'))
        self.assertEqual(second['state']['config']['command_timeout'],300)

    def test_revise_requirement_requires_a_stopped_development_ticket(self):
        with self.assertRaisesRegex(ValueError,'stopped development ticket'):
            act(self.tickets,self.base|{'action':'revise_requirement'},'revise-intake-1')
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'revise-running-start')
        with self.assertRaisesRegex(ValueError,'stopped development ticket'):
            act(self.tickets,self.base|{'action':'revise_requirement'},'revise-running-1')

    @patch('graph_engineering.workflow.ModelClient.health',return_value={'available':True})
    def test_regression_reviewer_sees_operator_notes_and_feature_framing(self,_):
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'review-notes-start')
        state=self.tickets.get(self.ticket['ticket_id'])['state']
        self.tickets.store.checkpoint(self.run['id'],'development_setup',
            {'error':'Attempt budget exhausted','triage':state.get('triage',{})|{'category':'feature'}},'development_setup','blocked')
        act(self.tickets,self.base|{'action':'development_information','text':'Drop the role feature.'},'review-notes-note')
        self.tickets.command(self.ticket['ticket_id'],'retry',{},'review-notes-retry')
        class ReviewDouble(DevelopmentDouble):
            prompt=''
            def invoke(self,role,*args,**kwargs):
                if role=='regression_review':ReviewDouble.prompt=args[0]
                return super().invoke(role,*args,**kwargs)
        flow=Workflow(self.home);flow.worker=ReviewDouble()
        try:flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertIn('Drop the role feature.',ReviewDouble.prompt)
        self.assertIn('NEW FEATURE',ReviewDouble.prompt)

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
            self.assertIn('PASSED on the original source',run['state']['regression_feedback'])  # It says why.
        finally:flow.store.close()
        # GE-30D05C40: retrying a rejected regression writes a new test with the feedback, never re-runs the old one.
        self.tickets.command(self.ticket['ticket_id'],'retry',{},'regression-retry-1')
        state=self.tickets.store.get(self.run['id'])
        # A fresh snapshot: development_setup discards the old checkout (and any leftover proposal) and rebuilds evidence.
        self.assertEqual((state['node'],state['state']['regression_validation_round']),('development_setup',0))
        # Setup's own attempt count is reset too, or a second retry would block on "Attempt budget exhausted".
        self.assertTrue(all(v==0 for k,v in state['state']['attempts'].items() if k.startswith('development_setup:')))
        self.assertNotIn('regression',state['state'])
        self.assertIn('PASSED on the original source',state['state']['regression_feedback'])

    def test_failed_implementation_replans_with_the_failure_and_the_regression(self):
        """GE-30D05C40: the plan named one file, the test needed another; repair must be able to widen the plan."""
        class WrongFirst(DevelopmentDouble):
            def __init__(self):super().__init__();self.plans=[];self.implementations=0
            def invoke(self,role,prompt,*args,**kwargs):
                if role=='planning':self.plans.append(json.loads(prompt))
                result=super().invoke(role,prompt,*args,**kwargs)
                if role=='implementation':
                    self.implementations+=1  # The implementation checkout keeps the previous attempt's edit.
                    result['content']['edits']=[{'path':'calc.py','old':'return a-b','new':'return a*b'}] if self.implementations==1 \
                        else [{'path':'calc.py','old':'return a*b','new':'return a+b'}]
                return result
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'replan-start-1')
        flow=Workflow(self.home);worker=WrongFirst();flow.worker=worker
        try:run=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
        self.assertEqual(len(worker.plans),2)  # Repair went back to planning, not straight to implementation.
        self.assertIn('regression_test',worker.plans[0])
        self.assertNotIn('previous_attempt',worker.plans[0])
        self.assertEqual(worker.plans[1]['previous_attempt']['files'],['calc.py'])
        self.assertIn('AssertionError',worker.plans[1]['previous_attempt']['failure_output'])

    def test_retrying_an_escalated_development_plans_again_with_a_fresh_repair_budget(self):
        class AlwaysWrong(DevelopmentDouble):
            wrong=['return a-b','return a*b','return a//b','return a%b','return a**b']
            def __init__(self):super().__init__();self.implementations=0
            def invoke(self,role,*args,**kwargs):
                result=super().invoke(role,*args,**kwargs)
                if role=='implementation':  # Each attempt edits the previous one, and is still wrong.
                    old,new=self.wrong[self.implementations],self.wrong[self.implementations+1];self.implementations+=1
                    result['content']['edits']=[{'path':'calc.py','old':old,'new':new}]
                return result
        act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'escalate-start-1')
        flow=Workflow(self.home);flow.worker=AlwaysWrong()
        try:run=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual((run['node'],run['status']),('done','escalated'))
        self.tickets.command(self.ticket['ticket_id'],'retry',{},'escalate-retry-1')
        state=self.tickets.store.get(self.run['id'])
        self.assertEqual((state['node'],state['status'],state['state']['repairs']),('task_planning','ready',0))
        self.assertNotIn('outcome',state['state'])
        # The retried budget must ask for new proposals, not replay the escalated ones (review of PR #6).
        class FixesFromLastAttempt(DevelopmentDouble):
            def __init__(self):super().__init__();self.plans=[]
            def invoke(self,role,prompt,*args,**kwargs):
                if role=='planning':self.plans.append(json.loads(prompt))
                result=super().invoke(role,prompt,*args,**kwargs)
                if role=='implementation':  # Fix whatever the escalated attempts left in the checkout.
                    current=next(l.split(': ',1)[1] for l in (home/'worktrees'/run_id/'implementation'/'calc.py').read_text().splitlines() if 'return' in l)
                    result['content']['edits']=[{'path':'calc.py','old':current,'new':'return a+b'}]
                return result
        home,run_id=self.home,self.run['id']
        self.tickets.db.execute('UPDATE tickets SET not_before=0 WHERE id=?',(self.ticket['ticket_id'],));self.tickets.db.commit()
        flow=Workflow(self.home);worker=FixesFromLastAttempt();flow.worker=worker
        try:run=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
        self.assertIn('previous_attempt',worker.plans[0])  # The retry plans from the escalated failure.

    def test_replan_within_a_round_keeps_the_previous_files_and_their_edits(self):
        """GE-30D05C40 (fourth stop): re-plans oscillated between the two halves of the change, each narrowing
        reverting the other half's edits. A repair now keeps the previous plan's files; only a fresh round after
        escalation starts over (test_escalation_retry... covers that)."""
        (self.repo/'util.py').write_text("from calc import add\ndef double(x): return add(x,x)\n")
        git(self.repo,'add','.');git(self.repo,'commit','-m','util')
        from graph_engineering.report_commands import act as _act
        self.tickets.store.db.execute("UPDATE runs SET state=json_set(state,'$.base_sha',?) WHERE id=?",(git(self.repo,'rev-parse','HEAD').strip(),self.run['id']))
        self.tickets.store.db.commit()
        class Narrowing(DevelopmentDouble):
            def __init__(self):super().__init__();self.plans=0;self.implementations=0;self.last_planning_prompt=None;self.rejections=[];self.current=[]
            def invoke(self,role,*args,**kwargs):
                result=super().invoke(role,*args,**kwargs)
                if role=='implementation':
                    prompt=json.loads(args[0]);self.rejections.append('rejected_proposal' in prompt);self.current.append(prompt.get('current_edits',{}).get('diff',''))
                if role=='planning':
                    self.last_planning_prompt=args[0]
                    self.plans+=1;result['content']['files']=['calc.py','util.py'] if self.plans==1 else ['calc.py']
                if role=='implementation':
                    self.implementations+=1
                    result['content']['edits']=[{'path':'calc.py','old':'return a-b','new':'return a*b'},
                                                {'path':'util.py','old':'add(x,x)','new':'add(x,x) # touched'}] if self.implementations==1 \
                        else [{'path':'calc.py','old':'return a*b','new':'return a+b'}]
                return result
        _act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'narrow-start-1')
        # Second review of PR #12 (F2): a refusal left in the state is fed back to the next proposal only, never replayed.
        self.tickets.store.db.execute("UPDATE runs SET state=json_set(state,'$.last_error',json(?)) WHERE id=?",(json.dumps({'message':'Adapted test lost assertions: t.py'}),self.run['id']))
        self.tickets.store.db.commit()
        flow=Workflow(self.home);flow.worker=Narrowing()
        try:run=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
        self.assertIn('# touched',run['state']['diff'])  # util.py stayed planned, so its first-attempt edit survived.
        self.assertEqual(run['state']['plan']['files'],['calc.py','util.py'])
        events=self.tickets.store.events(self.run['id'])
        self.assertEqual([e['data']['kept'] for e in events if e['kind']=='plan_widened'],[['util.py']])
        self.assertNotIn('unplanned_edit_reverted',[e['kind'] for e in events])
        widened=json.loads(flow.worker.last_planning_prompt)
        self.assertIn('note',widened['previous_attempt'])
        self.assertEqual((flow.worker.rejections,run['state'].get('last_error')),([True,False],{}))
        self.assertEqual((flow.worker.current[0],'# touched' in flow.worker.current[1]),('',True))  # The second proposal sees the first's edits.

    def test_rejection_that_contradicts_the_passing_test_run_is_reconsidered_once(self):
        """GE-30D05C40: the 9B reviewer read the pre-change baseline ("actual: undefined") as the current result and
        rejected a passing change. The prompt now states the controller's run as facts; a rejection while the suite
        passed is re-asked once with the contradiction, both answers recorded, the second decision standing."""
        from graph_engineering.report_commands import act as _act
        class MisreadingReviewer(DevelopmentDouble):
            def __init__(self):super().__init__();self.prompts=[]
            def invoke(self,role,*args,**kwargs):
                if role!='review':return super().invoke(role,*args,**kwargs)
                prompt=json.loads(args[0]);self.prompts.append(prompt)
                if 'reconsider' in prompt:content={'decision':'approve','findings':[],'rationale':'The suite passed; the finding relied on the pre-change output.'}
                else:content={'decision':'request_changes','findings':[{'severity':'critical','file':'calc.py','line':1,'message':'The test still fails: actual undefined'}],'rationale':'test fails'}
                metric={'role':role,'success':True,'model_key':'b','model_id':'misreading-reviewer','latency_seconds':0}
                self.metrics.append(metric);return {'content':content,'metrics':metric,'raw_path':'misreading-reviewer'}
        _act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'reconsider-start-1')
        flow=Workflow(self.home);worker=MisreadingReviewer();flow.worker=worker
        try:run=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual(run['status'],'ready_for_delivery',run['state'].get('error'))
        first,second=worker.prompts
        self.assertEqual((first['verification']['full_suite_passed'],first['verification']['regression_test_passed']),(True,True))
        self.assertTrue(first['output_before_change'].startswith('At the accepted commit, BEFORE this diff'))
        self.assertNotIn('baseline_output',first);self.assertIn('test_output_after_change',first)
        self.assertEqual(second['prior_review']['decision'],'request_changes')
        self.assertEqual(run['state']['review']['prior_review']['findings'][0]['severity'],'critical')  # Kept for audit.
        self.assertEqual([e['data']['second_decision'] for e in self.tickets.store.events(self.run['id']) if e['kind']=='review_reconsidered'],['approve'])

    def test_rejection_for_a_real_defect_is_not_reconsidered_and_a_renewed_rejection_stands(self):
        """Review of PR #14: only findings that cite a test failing trigger the re-ask; a renewed rejection goes to
        repair with the superseded findings kept off the planner prompt; a failed second ask leaves the first standing."""
        from graph_engineering.models import ModelError
        from graph_engineering.report_commands import act as _act
        from graph_engineering.workflow import contradicts_test_run
        self.assertFalse(contradicts_test_run({'findings':[{'severity':'critical','message':'Division by zero when b is 0'}]}))
        self.assertTrue(contradicts_test_run({'findings':[{'severity':'required','message':'The regression test fails: actual undefined'}]}))
        self.assertFalse(contradicts_test_run({'findings':[{'severity':'minor','message':'the test fails'}]}))  # Not blocking.
        worktree=self.home/'worktrees'/self.run['id']/'implementation'
        class Reviewer(DevelopmentDouble):
            def __init__(self,answers):super().__init__();self.answers=list(answers);self.prompts=[]
            def invoke(self,role,*args,**kwargs):
                if role=='planning':self.prompts.append(('planning',json.loads(args[0])))
                if role=='implementation':
                    result=super().invoke(role,*args,**kwargs)
                    if 'return a+b' in (worktree/'calc.py').read_text():result['content']['edits']=[{'path':'calc.py','old':'def add','new':'def add'}]  # Already fixed this round.
                    return result
                if role!='review':return super().invoke(role,*args,**kwargs)
                prompt=json.loads(args[0]);self.prompts.append(('review',prompt));answer=copy.deepcopy(self.answers.pop(0))
                if isinstance(answer,Exception):raise answer
                metric={'role':role,'success':True,'model_key':'b','model_id':'reviewer','latency_seconds':0}
                self.metrics.append(metric);return {'content':answer,'metrics':metric,'raw_path':'reviewer'}
        reject_test={'decision':'request_changes','findings':[{'severity':'critical','file':'calc.py','line':1,'message':'The test still fails: actual undefined'}],'rationale':'r'}
        reject_logic={'decision':'request_changes','findings':[{'severity':'critical','file':'calc.py','line':1,'message':'Overflow for large inputs'}],'rationale':'r'}
        approve={'decision':'approve','findings':[],'rationale':'ok'}
        _act(self.tickets,self.base|{'action':'start_development','trust_execution':True},'renewed-start-1')
        # 1st review: cites a test -> re-asked, rejected again (stands) -> repair; 2nd review: logic defect -> no re-ask -> repair;
        # 3rd review: cites a test -> second ask fails non-transiently -> first stands -> escalation (max_repairs 2).
        worker=Reviewer([reject_test,reject_test,reject_logic,reject_test,ModelError('Input exceeds selective-retrieval byte budget')])
        flow=Workflow(self.home);flow.worker=worker
        try:run=flow.execute(self.run['id'])
        finally:flow.store.close()
        self.assertEqual(run['status'],'escalated',run['state'].get('error'))
        reviews=[p for kind,p in worker.prompts if kind=='review'];plans=[p for kind,p in worker.prompts if kind=='planning']
        self.assertEqual([('reconsider' in p) for p in reviews],[False,True,False,False,True])
        self.assertIn('request_changes again is the correct answer',reviews[1]['reconsider'])
        self.assertNotIn('prior_review',plans[1]['previous_attempt']['review_findings'])  # The superseded finding never reaches the planner.
        kinds=[e['kind'] for e in self.tickets.store.events(self.run['id'])]
        self.assertEqual((kinds.count('review_reconsidered'),kinds.count('review_reconsideration_failed')),(1,1))
        self.assertEqual(run['state']['review']['findings'][0]['message'],'The test still fails: actual undefined')  # The first answer stood.

    def test_github_publication_follows_the_repository_pr_convention(self):
        """Branch <type>/<issue>-<slug>, body with Closes #N and Change / Validation / Review sections (General_AI_Agent's PR policy)."""
        from graph_engineering.workflow import publication_branch,publication_body
        state={'triage':{'category':'feature'},'candidate':{'title':'Feature Request: Display & Select Current AI Model in Chat Interface!'},
               'patch':{'summary':'Expose the model name.'},'plan':{'files':['backend/agent.js','backend/llm.js'],'adapted_tests':['backend/tests/agent.test.js']},
               'head':'f'*40,'base_sha':'0'*40,'regression':{'path':'backend/tests/graph_x.test.js'},
               'verification':{'passed':True},'review':{'decision':'approve','rationale':'Correct.','model':'b'}}
        self.assertEqual(publication_branch(state,17),'feat/17-feature-request-display-select-current-ai-model')
        self.assertEqual(publication_branch(state|{'triage':{'category':'bug'},'candidate':{'title':'!!!'}},3),'fix/3-change')
        body=publication_body(state,17)
        for needle in ('Closes #17','## Change','## Validation','## Review','backend/tests/agent.test.js','passes at the tested head','approve'):
            self.assertIn(needle,body)
        self.assertLess(body.index('## Change'),body.index('## Validation'))

    def test_fragment_edits_reject_ambiguous_matches_and_outside_paths(self):
        from graph_engineering.accepted_development import materialize_hunks
        for edits in ([{'path':'../owned.py','old':'a','new':'b'}],[{'path':'calc.py','old':'a','new':'b'}]):
            with self.assertRaises(ValueError):materialize_hunks(self.repo,{'edits':edits},['calc.py'])
        # GE-30D05C40: the refusal names the file, the match count and the fragment so the next proposal can correct it.
        with self.assertRaisesRegex(ValueError,r"calc\.py has 0 matches for the fragment between <<< and >>>: <<<return a\*b>>>"):
            materialize_hunks(self.repo,{'edits':[{'path':'calc.py','old':'return a*b','new':'return a+b'}]},['calc.py'])
        with self.assertRaisesRegex(ValueError,r"first 160 of 200 characters"):
            materialize_hunks(self.repo,{'edits':[{'path':'calc.py','old':'z'*200,'new':'y'}]},['calc.py'])
        # An edit whose replacement is already in the file (applied by an earlier proposal this round) is skipped, not refused.
        base=git(self.repo,'rev-parse','HEAD').strip()
        (self.repo/'calc.py').write_text((self.repo/'calc.py').read_text().replace('return a-b','return a-b  # applied by an earlier proposal'))
        applied={'path':'calc.py','old':'return a-b\n','new':'return a-b  # applied by an earlier proposal\n'}
        skipped=[]
        out=materialize_hunks(self.repo,{'edits':[applied,{'path':'calc.py','old':'def add','new':'def add'}]},['calc.py'],skipped,base)
        self.assertEqual([s['path'] for s in skipped],['calc.py'])
        self.assertEqual(out[0]['path'],'calc.py')  # The second edit (old == new, present once) still applies as a no-op.
        with self.assertRaisesRegex(ValueError,'all already applied this round \\(calc.py\\)'):  # Nothing left: the reason is fed back.
            materialize_hunks(self.repo,{'edits':[applied]},['calc.py'],[],base)
        with self.assertRaises(ValueError):  # Without the skip list the refusal stands.
            materialize_hunks(self.repo,{'edits':[applied]},['calc.py'])
        # Review: text that was already in the file at the accepted commit, or too short to prove anything, is never "applied".
        with self.assertRaisesRegex(ValueError,'has 0 matches'):
            materialize_hunks(self.repo,{'edits':[{'path':'calc.py','old':'def sub(a,b)','new':'def add(a, b): return a-b  # applied by an earlier proposal'}]},['calc.py'],[],base)
        with self.assertRaisesRegex(ValueError,'has 0 matches'):
            materialize_hunks(self.repo,{'edits':[{'path':'calc.py','old':'return a-b;','new':'return a-b'}]},['calc.py'],[],base)
        git(self.repo,'checkout','--','calc.py')

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
