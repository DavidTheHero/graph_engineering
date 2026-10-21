"""Opt-in repository autonomy; accepted scope and existing merge gates remain binding.

With ``development`` on, the policy also owns recovery: budgets widen (``max_repairs``,
``max_node_attempts``) and a blocked or escalated development is retried automatically, with the
right restart (re-author, re-plan or fresh setup), for ``recovery_rounds`` rounds before a person
sees it. ``auto_accept`` accepts an investigated ticket whose references verified and that has no
open questions. Merge approval is never automatic here.
"""
import json
import re
from .runtime import digest,now

DEFAULT={'development':False,'publish':False,'prepare_dependencies':False,'auto_accept':False,'adapt_existing_tests':False,
         'recovery_rounds':0,'max_repairs':2,'max_node_attempts':2,'actor':None}
BUDGETS={'recovery_rounds':(0,6),'max_repairs':(1,4),'max_node_attempts':(1,4)}
# Failures a retry cannot change: the requirement, the policy or the repository must change first.
UNRECOVERABLE=('Accepted revision and explicit execution approval required','Regression review rejected twice',
               'Development setup requires a nonempty','Accepted report has no validated source component',
               'Implementation prompt (','Planned source files exceed','Planned source (','Regression path escapes','Regression changed outside',
               'Dependency setup is restricted')

def policy(tickets,repo_id):return DEFAULT|tickets.setting('autonomy:'+repo_id,{})

def configure(tickets,data,key):
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):raise ValueError('Idempotency key required')
    repo=data.get('repository_id');actor=data.get('actor')
    if not tickets.db.execute('SELECT 1 FROM registered_repositories WHERE id=?',(repo,)).fetchone():raise ValueError('Unknown repository')
    if not isinstance(actor,str) or not actor.strip() or len(actor)>120:raise ValueError('Named policy operator required')
    flags={k:data.get(k,False) for k in ('development','publish','prepare_dependencies','auto_accept','adapt_existing_tests')}
    if any(type(v) is not bool for v in flags.values()):raise ValueError('Autonomy settings must be boolean')
    if (flags['publish'] or flags['prepare_dependencies'] or flags['adapt_existing_tests']) and not flags['development']:raise ValueError('Execution policy must be enabled first')
    for name,(low,high) in BUDGETS.items():
        value=data.get(name,DEFAULT[name])
        if type(value) is not int or not low<=value<=high:raise ValueError(f'{name} must be an integer from {low} to {high}')
        flags[name]=value
    if flags['recovery_rounds'] and not flags['development']:raise ValueError('Automatic recovery requires the development policy')
    request=digest(data);db=tickets.db;db.execute('BEGIN IMMEDIATE')
    try:
        prior=db.execute('SELECT request_hash,result FROM operator_commands WHERE key=?',(key,)).fetchone()
        if prior:
            if prior[0]!=request:raise ValueError('Idempotency key reused')
            db.rollback();return json.loads(prior[1])
        result=flags|{'actor':actor.strip(),'updated':now()}
        db.execute('INSERT OR REPLACE INTO scheduler_settings VALUES(?,?)',('autonomy:'+repo,json.dumps(result)))
        tickets.store._event('controller','autonomy','repository_policy_changed',{'repository_id':repo,'policy':result})
        db.execute('INSERT INTO operator_commands VALUES(?,?,?)',(key,request,json.dumps(result)));db.commit();return result
    except BaseException:db.rollback();raise

def current(flow,state):
    key='autonomy:'+state['intake']['repository_id']
    row=flow.store.db.execute('SELECT value FROM scheduler_settings WHERE key=?',(key,)).fetchone()
    return DEFAULT|(json.loads(row[0]) if row else {})


def recoverable(ticket):
    """Whether an automatic retry of this blocked/escalated development can change the outcome."""
    if ticket.get('paused') or ticket.get('cancel') or ticket.get('lease_owner'):return False  # dispatch never touches these.
    state=ticket['state'];error=state.get('error')
    message=str(error.get('message','') if isinstance(error,dict) else error or '')
    if ticket['status']=='escalated':return state['config'].get('run_mode')=='accepted_development'
    if ticket['status']!='blocked' or state['config'].get('run_mode')!='accepted_development':return False
    return not message.startswith(UNRECOVERABLE)


def acceptable(ticket):
    """An investigated ticket with independently verified references and nothing left to ask."""
    if ticket.get('paused') or ticket.get('cancel') or ticket.get('lease_owner'):return False  # dispatch never touches these.
    state=ticket['state'];validation=state.get('finding_validation') or {};draft=state.get('enhanced') or {}
    return (ticket['status']=='ready_for_review' and state['config'].get('run_mode')=='intake'
            and bool(validation.get('source_references_valid') and validation.get('verified_references'))
            and not draft.get('questions') and state.get('revision') is not None)

def dispatch(tickets):
    import sqlite3
    from .report_commands import act
    for ticket in tickets.list():
        try:
            state=ticket['state'];repo=(state.get('intake') or {}).get('repository_id')
            if not repo or ticket['lease_owner'] or ticket.get('cancel') or ticket['paused']:continue
            if state.get('revision') is None:continue  # Not investigated yet: nothing for the policy to accept, start or recover.
            setting=policy(tickets,repo);cycle=f"{state['revision']}c{state.get('acceptance_cycle',0)}"  # Per acceptance cycle.
            if setting['auto_accept'] and acceptable(ticket):
                act(tickets,{'ticket_id':ticket['id'],'revision':state['revision'],'actor':setting['actor'],'action':'accept',
                    'autonomy_policy_updated':setting['updated']},'autoaccept-'+ticket['id']+'-'+cycle)
                continue
            if not setting['development']:continue
            if ticket['status'] in ('ready_for_development','development_queued') and state.get('accepted_revision')==state.get('revision'):
                act(tickets,{'ticket_id':ticket['id'],'revision':state['revision'],'actor':setting['actor'],
                    'action':'start_development','trust_execution':True,'autonomy_policy_updated':setting['updated']},
                    'autostart-'+ticket['id']+'-'+cycle+'-'+digest(setting)[:8])
            elif ticket['status'] in ('blocked','escalated') and recoverable(ticket):
                recover(tickets,ticket,setting)
        except sqlite3.OperationalError:
            raise  # Lock contention is the scheduler's to throttle and report, not a ticket's to record.
        except ValueError as exc:
            # Operator actions can race the snapshot above. Reconsider next sweep,
            # retaining one receipt per reason instead of terminating the service.
            reason='Autonomous admission deferred: '+str(exc)
            if tickets.get(ticket['id'])['last_reason']!=reason:
                with tickets.db:
                    tickets.db.execute('UPDATE tickets SET last_reason=? WHERE id=?',(reason,ticket['id']))
                    tickets.store._event(ticket['run_id'],ticket['node'],'autonomy_admission_deferred',{'reason':reason})
        except Exception as exc:
            # One ticket's unexpected state must never stop the sweep for the others (GE-FDD2275A, 2026-10-21: a
            # KeyError here killed the worker service). Recorded once per distinct error, without overwriting the
            # ticket's own block reason.
            error=f'{type(exc).__name__}: {str(exc)[:200]}'
            last=tickets.db.execute("SELECT data FROM events WHERE run_id=? AND kind='autonomy_sweep_error' ORDER BY rowid DESC LIMIT 1",(ticket['run_id'],)).fetchone()
            if not last or json.loads(last[0]).get('error')!=error:
                with tickets.db:
                    tickets.store._event(ticket['run_id'],ticket['node'],'autonomy_sweep_error',{'error':error})


def recover(tickets,ticket,setting):
    """One bounded automatic recovery: a retry with the right restart, within the policy's rounds.

    Each round is recorded on the ticket. Rounds the policy grants extend the ticket's retry limit by
    one when it is exhausted, so the visible ``retries n/m`` stays honest. Rounds already used by a
    previous policy value are kept, so lowering the policy never grants more."""
    state=ticket['state'];used=state.get('autonomy_recoveries',0)
    if used>=setting['recovery_rounds']:return False
    run_id=ticket['run_id']
    with tickets.db:
        if ticket['retries']>=ticket['max_retries']:
            tickets.db.execute('UPDATE tickets SET max_retries=max_retries+1 WHERE id=?',(ticket['id'],))
            tickets.store._event(run_id,ticket['node'],'retry_budget_extended',{'from':ticket['max_retries'],'to':ticket['max_retries']+1,
                'actor':setting['actor'],'reason':'automatic recovery round %d/%d under the repository policy'%(used+1,setting['recovery_rounds'])})
        tickets.db.execute("UPDATE runs SET state=json_set(state,'$.autonomy_recoveries',?) WHERE id=?",(used+1,run_id))
        tickets.store._event(run_id,ticket['node'],'autonomy_recovery',{'round':used+1,'limit':setting['recovery_rounds'],'actor':setting['actor'],
            'status':ticket['status'],'error':str((state.get('error') or {}).get('message','') if isinstance(state.get('error'),dict) else state.get('error') or '')[:300]})
    try:
        tickets.command(ticket['id'],'retry',{},'autorecover-'+ticket['id']+'-'+f"{state.get('acceptance_cycle',0)}-{used+1}")
    except BaseException:
        # No retry happened: give the round (and any extension) back, so a race never burns the policy's rounds.
        with tickets.db:
            tickets.db.execute('UPDATE tickets SET max_retries=? WHERE id=?',(ticket['max_retries'],ticket['id']))
            tickets.db.execute("UPDATE runs SET state=json_set(state,'$.autonomy_recoveries',?) WHERE id=?",(used,run_id))
            tickets.store._event(run_id,ticket['node'],'autonomy_recovery_reverted',{'round':used+1})
        raise
    return True
