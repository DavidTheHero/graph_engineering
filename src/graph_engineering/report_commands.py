"""Revision-bound human actions. No external writes occur in HTTP handlers."""
import json
import re

from .report_store import INVESTIGATION_BUDGET,report,revision
from .runtime import digest,now


def act(tickets,data,key):
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):raise ValueError('Idempotency key required')
    request_hash=digest(['intake-action',data]);db=tickets.db;db.execute('BEGIN IMMEDIATE')
    try:
        prior=db.execute('SELECT request_hash,result FROM operator_commands WHERE key=?',(key,)).fetchone()
        if prior:
            if prior[0]!=request_hash:raise ValueError('Idempotency key reused for another command')
            db.rollback();return json.loads(prior[1])
        ticket=tickets.get(data['ticket_id']);state=ticket['state'];original=report(db,ticket['id'])
        if not original:raise ValueError('This ticket has no natural-language report')
        if ticket['lease_owner']:raise ValueError('Pause and wait for the current worker checkpoint before editing or review')
        current=state.get('revision',0)
        if type(data.get('revision')) is not int or data['revision']!=current:raise ValueError('Ticket revision changed; reload before acting')
        action=data.get('action');actor=data.get('actor','')
        if ticket.get('cancel') or ticket['status']=='cancelled':raise ValueError('Ticket is cancelled. Use Retry with backoff explicitly before another action.')
        if state['config'].get('run_mode')=='accepted_development' and action not in ('publish_development','development_information','answer_questions'):raise ValueError('Development has started with a frozen accepted revision; add troubleshooting information, ask Qwen 4B, or use pause, cancel or retry to control execution.')
        if not isinstance(actor,str) or not actor.strip() or len(actor)>120:raise ValueError('A named operator is required')
        actor=actor.strip()
        node=ticket['node'];status=ticket['status']
        if action=='answer_questions':
            questions=state.get('enhanced',{}).get('questions',[])
            # Either a ticket blocked during execution, or an investigated intake ticket waiting for clarification.
            waiting=state['config'].get('run_mode')=='intake' and status=='needs_information' and node=='ticket_finalisation'
            if status!='blocked' and not waiting:raise ValueError('Qwen 4B question answering requires a blocked ticket or an investigated ticket waiting for clarification')
            if not isinstance(questions,list) or not questions:raise ValueError('This ticket has no persisted unresolved questions')
            if waiting and state['intake'].get('generation',0)>=INVESTIGATION_BUDGET:raise ValueError('Further-investigation budget exhausted; create a scoped follow-up ticket')
            state['question_answer_request']={'return_node':node,'return_error':state.get('error'),'mode':'clarification' if waiting else 'blocked',
                'requested_by':actor,'requested_at':now(),'questions_hash':digest(questions)}
            state['attempts']={k:v for k,v in state.get('attempts',{}).items() if not k.startswith('question_answering:')}
            node,status='question_answering','ready'
        elif action=='development_information':
            if state['config'].get('run_mode')!='accepted_development' or status!='blocked':raise ValueError('Troubleshooting information requires a blocked development ticket at a safe checkpoint')
            text=data.get('text');notes=state.get('development_information',[])
            if not isinstance(text,str) or not text.strip() or len(text)>4000:raise ValueError('Provide troubleshooting information in 1–4000 characters')
            if len(notes)>=5:raise ValueError('Five troubleshooting notes already recorded; create a scoped follow-up for further changes')
            state['development_information']=[*notes,{'text':text,'actor':actor,'at':now()}]
        elif action=='edit_enhancement':
            content=data.get('changes');allowed={'title','problem_statement','expected_behaviour','observed_behaviour',
                'root_cause_hypothesis','proposed_solution','acceptance_criteria','validation_plan','reproduction_steps'}
            if not isinstance(content,dict) or not content or not set(content)<=allowed:raise ValueError('Only technical prose and criteria are editable; original report and evidence are immutable')
            if not state.get('enhanced'):raise ValueError('No enhanced draft exists yet')
            for field,value in content.items():
                if field in ('acceptance_criteria','validation_plan','reproduction_steps'):
                    if not isinstance(value,list) or not 1<=len(value)<=20 or any(not isinstance(x,str) or not x.strip() or len(x)>1000 for x in value):raise ValueError('Criteria must be a nonempty bounded list')
                elif not isinstance(value,str) or not value.strip() or len(value)>(180 if field=='title' else 8000):raise ValueError('Invalid edited text')
            number,enhanced=revision(db,ticket['id'],'operator:'+key,state['enhanced']|content,
                state.get('finding_validation',{}),actor)
            state.update(enhanced=enhanced,revision=number,accepted_revision=None,publication_revision=None)
            node,status='ticket_finalisation','ready_for_review'
        elif action=='accept':
            validation=state.get('finding_validation',{})
            if status!='ready_for_review' or not validation.get('source_references_valid') or not validation.get('verified_references'):
                raise ValueError('A draft with independently checked source references is required; runtime cause remains a hypothesis')
            state.update(accepted_revision=current,intake_phase='Ready for Development')
            node,status='development_admission','ready_for_development'
        elif action in ('clarify','reinvestigate'):
            if state['intake'].get('generation',0)>=INVESTIGATION_BUDGET:raise ValueError('Further-investigation budget exhausted; create a scoped follow-up ticket')
            text=data.get('text','')
            if not isinstance(text,str) or len(text)>4000 or action=='clarify' and not text.strip():raise ValueError('Provide clarification in at most 4000 characters')
            state['clarifications']=[*state.get('clarifications',[]),{'actor':actor,'at':now(),'text':text}]
            state['intake']['generation']=state['intake'].get('generation',0)+1
            state.update(search_round=0,validation_round=0,attempts={},accepted_revision=None,publication_revision=None)
            state.pop('error',None);state.pop('investigation_result',None);state.pop('finding_validation',None)
            node,status=('repository_search' if state.get('triage') and state.get('base_sha') else 'manual_intake'),'ready'
        elif action=='reject_enhancement':
            state['accepted_revision']=None;status='enhancement_rejected'
        elif action=='mark_duplicate':
            related=data.get('related_ticket')
            if related==ticket['id']:raise ValueError('A ticket cannot duplicate itself')
            tickets.get(related);state['duplicate_of']=related;status='duplicate'
        elif action=='send_development':
            if not current or state.get('accepted_revision')!=current:raise ValueError('Accept the current enhanced revision first')
            state.update(intake_phase='Development Queued',development={'admitted':True,'actor':actor,'revision':current,'implementation_started':False})
            node,status='development_admission','development_queued'
        elif action=='start_development':
            if 'autonomy_policy_updated' in data:
                from .autonomy import policy
                setting=policy(tickets,original['repository_id'])
                if not setting['development'] or setting.get('updated')!=data['autonomy_policy_updated']:
                    raise ValueError('Repository autonomy policy changed before admission')
            if status not in ('ready_for_development','development_queued') or state.get('accepted_revision')!=current or data.get('trust_execution') is not True:
                raise ValueError('Accept the current revision and explicitly authorise isolated repository code execution first')
            state['development']={'admitted':True,'authorised_revision':current,'actor':actor,'implementation_started':True}
            state['config'].update(run_mode='accepted_development',trusted_local=True,auto_merge=False)
            state.pop('error',None);node,status='development_setup','ready'
        elif action=='publish_development':
            if status!='ready_for_delivery' or data.get('approve_publication') is not True or state.get('accepted_revision')!=state['development']['authorised_revision']:
                raise ValueError('Reviewed, tested accepted revision and explicit delivery approval required')
            state['delivery_revision']=state['accepted_revision'];node,status='development_delivery','ready'
        elif action=='publish':
            if not current or state.get('accepted_revision')!=current or data.get('approve_publication') is not True:
                raise ValueError('Explicit publication approval of the accepted current revision is required')
            if state['config']['backend']!='github' and data.get('simulation') is not True:
                raise ValueError('GitHub publication is not configured; explicitly choose a local simulation or configure the authorised GitHub backend')
            if not (state.get('issue') and state.get('published_revision')==current):
                state.update(publication_actor=actor,publication_revision=current)
                node,status='ticket_publication','ready'
        else:raise ValueError('Unsupported intake action')
        db.execute('UPDATE runs SET node=?,status=?,state=?,cancel=0,updated=? WHERE id=?',
                   (node,status,json.dumps(state),now(),ticket['run_id']))
        if action!='development_information':db.execute("UPDATE tickets SET paused=0,last_reason='' WHERE id=?",(ticket['id'],))
        tickets.store._event(ticket['run_id'],ticket['node'],'intake_'+action,
            {'actor':actor,'revision':state.get('revision'),'source':ticket['node'],'destination':node,'status':status})
        result={'ticket_id':ticket['id'],'revision':state.get('revision'),'status':status,'accepted':True}
        db.execute('INSERT INTO operator_commands VALUES(?,?,?)',(key,request_hash,json.dumps(result)))
        db.commit();return result
    except BaseException:db.rollback();raise
