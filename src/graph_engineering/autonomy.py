"""Opt-in repository autonomy; accepted scope and existing merge gates remain binding."""
import json
import re
from .runtime import digest,now

DEFAULT={'development':False,'publish':False,'prepare_dependencies':False,'actor':None}

def policy(tickets,repo_id):return tickets.setting('autonomy:'+repo_id,DEFAULT.copy())

def configure(tickets,data,key):
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):raise ValueError('Idempotency key required')
    repo=data.get('repository_id');actor=data.get('actor')
    if not tickets.db.execute('SELECT 1 FROM registered_repositories WHERE id=?',(repo,)).fetchone():raise ValueError('Unknown repository')
    if not isinstance(actor,str) or not actor.strip() or len(actor)>120:raise ValueError('Named policy operator required')
    flags={k:data.get(k,False) for k in ('development','publish','prepare_dependencies')}
    if any(type(v) is not bool for v in flags.values()):raise ValueError('Autonomy settings must be boolean')
    if (flags['publish'] or flags['prepare_dependencies']) and not flags['development']:raise ValueError('Execution policy must be enabled first')
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
    return json.loads(row[0]) if row else DEFAULT.copy()

def dispatch(tickets):
    from .report_commands import act
    for ticket in tickets.list():
        state=ticket['state'];repo=state.get('intake',{}).get('repository_id')
        if not repo or ticket['lease_owner'] or ticket.get('cancel') or ticket['paused']:continue
        setting=policy(tickets,repo)
        if not setting['development']:continue
        try:
            if ticket['status'] in ('ready_for_development','development_queued') and state.get('accepted_revision')==state.get('revision'):
                act(tickets,{'ticket_id':ticket['id'],'revision':state['revision'],'actor':setting['actor'],
                    'action':'start_development','trust_execution':True,'autonomy_policy_updated':setting['updated']},
                    'autostart-'+ticket['id']+'-'+str(state['revision'])+'-'+digest(setting)[:8])
            elif ticket['status']=='blocked' and state['config'].get('run_mode')=='accepted_development' and ticket['retries']<ticket['max_retries']:
                error=state.get('error')
                retryable=(isinstance(error,dict) and (error.get('type')=='ModelError' or str(error.get('message','')).startswith('Locked dependency installation failed'))) or error=='Attempt budget exhausted'
                if retryable:tickets.command(ticket['id'],'retry',{},'autoretry-'+ticket['id']+'-'+str(ticket['retries']))
        except ValueError as exc:
            # Operator actions can race the snapshot above. Reconsider next sweep,
            # retaining one receipt per reason instead of terminating the service.
            reason='Autonomous admission deferred: '+str(exc)
            if tickets.get(ticket['id'])['last_reason']!=reason:
                with tickets.db:
                    tickets.db.execute('UPDATE tickets SET last_reason=? WHERE id=?',(reason,ticket['id']))
                    tickets.store._event(ticket['run_id'],ticket['node'],'autonomy_admission_deferred',{'reason':reason})
