"""One authoritative projection for the ticket board, queues and runtime graph."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import time

from .dashboard_data import Catalog, EVIDENCE, ROLE_NODES, bounded
from .runtime import redact
from .scheduling_policy import board_status, effective_rank
from .scheduler import required_model, ROLES
from .tickets import Tickets, QUEUES
from .workflow import graph_contracts
from .control_repositories import repositories


CLUSTERS = {
 'ingestion':('Ingestion',['manual_intake','repository_discovery','evidence_collection','issue_detection']),
 'classification':('Classification & queues',['request_triage','request_duplicate_check','issue_validation','deduplication','ticket_creation','prioritisation']),
 'planning':('Investigation & planning',['repository_search','problem_investigation','ticket_enhancement','repository_investigation','task_planning','model_selection']),
 'engineering':('Engineering workers',['isolated_implementation']),
 'quality':('Quality & review',['reproduction_check','finding_validation','test_execution','independent_review']),
 'delivery':('Delivery',['ticket_finalisation','ticket_publication','development_admission','pull_request_creation','ci_policy_verification','approval_gate','merge','post_merge_verification','completion']),
 'recovery':('Recovery',['repair','escalation'])}


def intake_lifecycle(ticket):
    statuses={'ready_for_review':'Ready for Review','needs_information':'Needs Information',
        'duplicate_review':'Possible Duplicate','ready_for_development':'Ready for Development',
        'development_queued':'Development Queued','enhancement_rejected':'Enhancement Rejected',
        'blocked':'Investigation Failed','cancelled':'Cancelled'}
    if ticket['status'] in statuses:return statuses[ticket['status']]
    return {'manual_intake':'New / Queued for Investigation','request_triage':'Classifying',
        'request_duplicate_check':'Classifying','repository_search':'Investigating','problem_investigation':'Investigating',
        'ticket_enhancement':'Enhancing','reproduction_check':'Validating','finding_validation':'Validating',
        'ticket_publication':'Publishing approved ticket'}.get(ticket['node'],'Development / '+ticket['node'].replace('_',' '))


def topology():
    contracts = graph_contracts()
    names = {c['name'] for c in contracts}
    edges = set()
    for c in contracts:
        for target in [c['next'],c['failure'],*c['conditional_edges']]:
            target = target.split(' ')[0]
            if target in names: edges.add((c['name'],target))
    # CandidateError requery is a real executable transition beyond the generic
    # validation contract's declared invalid-finding terminal route.
    edges.add(('issue_validation','issue_detection'))
    return {'nodes':[dict(id=c['name'],label=c['name'].replace('_',' ').title(),
                        cluster=next(k for k,(_,items) in CLUSTERS.items() if c['name'] in items),contract=c) for c in contracts],
            'edges':[{'id':a+'~'+b,'source':a,'target':b,'kind':'workflow'} for a,b in sorted(edges)],
            'clusters':[{'id':k,'label':v[0]} for k,v in CLUSTERS.items()],
            'queues':[{'id':'queue:'+k,'label':v,'cluster':'classification'} for k,v in QUEUES.items() if k not in ('done','active')],
            'parallelism':'Independent ticket processes. Child tasks exist only when observed in real quality events.'}


def normalize_event(event, ticket):
    data = event['data']
    node = data.get('graph_node') or ROLE_NODES.get(event['node'],event['node'])
    return redact({'event_id':event['seq'],'timestamp':event['at'],'run_id':event['run_id'],
        'ticket_id':ticket['id'] if ticket else data.get('ticket_id'),
        'demonstration_notice':ticket['state']['config'].get('demo_notice') if ticket else None,
        'execution_attempt_id':data.get('execution_attempt_id'),
        'parent_task_id':data.get('parent_task_id') or (ticket['id'] if ticket else None),
        'child_task_id':data.get('child_task_id'),
        'source_node':data.get('source',node),
        'destination_node':data.get('destination',data.get('next',node)),
        'event_type':event['kind'],'agent_id':data.get('agent_id') or data.get('role'),
        'model_id':data.get('model_id'),'model_key':data.get('model_key'),
        'status':data.get('status', 'failed' if event['kind'] in ('failed','worker_failed') else
            'running' if event['kind'] in ('started','model_assigned','child_started') else 'observed'),
        'artifact_references':[data[k] for k in ('artifact','raw_path') if isinstance(data.get(k),str)],
        'data':bounded(data)})


def events(tickets, after=0, limit=250, ticket_id=None):
    lookup = {t['run_id']:t for t in tickets.list()}
    if ticket_id:
        ticket = tickets.get(ticket_id)
        rows = tickets.db.execute('SELECT * FROM events WHERE seq>? AND run_id=? ORDER BY seq LIMIT ?',
                                  (after,ticket['run_id'],limit))
    else: rows = tickets.db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?', (after,limit))
    return [normalize_event(dict(r)|{'data':json.loads(r['data'])},lookup.get(r['run_id'])) for r in rows]


def snapshot(home):
    tickets = Tickets(home)
    try:
        tickets.db.execute('BEGIN')
        rows = tickets.list(); all_tickets = {t['id']:t for t in rows}
        result = []
        for ticket in rows:
            state = ticket['state']; config = state['config']
            queue,reason = tickets.queue(ticket,all_tickets)
            if queue in ('high','normal','low') and ticket['last_reason']:
                queue,reason = 'resource',ticket['last_reason']
            candidate = state.get('candidate',{})
            manual=tickets.db.execute('SELECT original FROM ticket_reports WHERE ticket_id=?',(ticket['id'],)).fetchone()
            enhanced=state.get('enhanced',{})
            assigned = tickets.db.execute("SELECT data,at FROM events WHERE run_id=? AND kind='model_assigned' ORDER BY seq DESC LIMIT 1",(ticket['run_id'],)).fetchone()
            assignment = json.loads(assigned[0]) if assigned else {}
            role = ROLES.get(ticket['node'],(None,None))[0]
            current_model = assignment.get('model_id') if ticket['lease_owner'] and assignment.get('role') == role else None
            position = ticket['node'] if queue in ('active','done') else 'queue:'+queue
            result.append(bounded({
                'id':ticket['id'],'run_id':ticket['run_id'],'title':enhanced.get('title') or state.get('triage',{}).get('title') or candidate.get('title') or (manual['original'].strip()[:120] if manual else 'Source investigation' if config.get('inspect_with_model') else 'Repository scan'),
                'description':enhanced.get('problem_statement') or (manual['original'] if manual else None) or candidate.get('actual') or state.get('investigation',{}).get('summary') or 'Inspect repository evidence and validate actionable findings.',
                'intake':bool(manual),'lifecycle':intake_lifecycle(ticket) if manual else None,
                'enhancement_revision':state.get('revision'),'accepted_revision':state.get('accepted_revision'),
                'repository':config.get('source_repo',ticket['repo']),'repository_name':Path(config.get('source_repo',ticket['repo'])).name,
                'revision':state.get('base_sha'),'category':ticket['category'],'labels':ticket['labels'],
                'severity':ticket['severity'],'confidence':ticket['confidence'],'priority':ticket['priority'],
                'board_status':board_status(ticket['node'],ticket['status'],bool(ticket['paused']) and not ticket['lease_owner']),
                'run_status':ticket['status'],'node':ticket['node'],'queue':queue,'queue_reason':reason,
                'effective_rank':effective_rank(ticket['priority'],ticket['created'],time.time()),
                'dependencies':ticket['dependencies'],'paused':bool(ticket['paused']),
                'retries':ticket['retries'],'repairs':state.get('repairs',0),
                'attempts':state.get('attempts',{}),'not_before':ticket['not_before'],
                'agent':role or 'deterministic','model':current_model,'planned_model':required_model(ticket),
                'execution_id':ticket['lease_owner'],'started':ticket['lease_started'] if ticket['lease_owner'] else None,
                'created':ticket['created'],'updated':ticket['run_updated'],'position':position,
                'branch':state.get('branch'),'worktree':state.get('worktree'),
                'issue':state.get('issue'),'pr':state.get('pr'),'backend':config.get('backend'),
                'tests':{k:v for k,v in state.get('verification',{}).items() if k in ('passed','returncode','duration_seconds','check_type')},
                'review':{k:v for k,v in state.get('review',{}).items() if k in ('approved','decision','model')},'error':state.get('error'),
                'mode':config.get('run_mode','development'),'approval_head':state.get('head'),
                'demonstration_notice':config.get('demo_notice'),
                'last_model_receipt':assignment if current_model else None}))
        health = [dict(key=r['key'],observed=r['observed'],**json.loads(r['data'])) for r in tickets.db.execute('SELECT * FROM model_observations')]
        children = {}
        for row in tickets.db.execute("SELECT * FROM events WHERE kind IN ('child_started','child_completed','child_failed') ORDER BY seq DESC LIMIT 200"):
            event = dict(row)|{'data':json.loads(row['data'])};child = event['data'].get('child_task_id')
            if child and child not in children:
                parent = next((t for t in rows if t['run_id']==row['run_id']),None)
                children[child] = normalize_event(event,parent)
        cursor = tickets.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        tickets.db.commit()
        return redact({'tickets':result,'children':list(children.values()),'queues':[{'id':k,'label':v,'count':sum(t['queue']==k for t in result)} for k,v in QUEUES.items()],
            'repositories':repositories(tickets),'models':health,'cursor':cursor,'topology':topology(),
            'scheduler':{'paused':tickets.setting('paused'),'workers':tickets.setting('workers'),
                         'active':sum(bool(t['execution_id']) for t in result),'model_capacity':tickets.setting('model_capacity'),
                         'service':tickets.setting('worker_service')},
            'statistics':{'completed':sum(t['board_status']=='Done' for t in result),
                          'failed':sum(t['board_status']=='Failed' for t in result),
                          'retried':sum(t['retries']+t['repairs'] for t in result)},
            'generated_at':time.time()})
    finally: tickets.store.close()


def ticket_detail(home, ticket_id):
    tickets = Tickets(home)
    try:
        ticket = tickets.get(ticket_id)
        detail = Catalog(home).detail('live',ticket['run_id'])
        detail['ticket_id'] = ticket_id
        from .report_store import report
        detail['intake_report']=report(tickets.db,ticket_id)
        detail['investigation']={key:ticket['state'][key] for key in ('triage','retrieved','enhanced',
            'finding_validation','reproduction','duplicate_candidates','revision','accepted_revision','published_revision','clarifications') if key in ticket['state']}
        detail['timeline'] = events(tickets,limit=2000,ticket_id=ticket_id)
        detail['worker_artifacts'] = []
        for call in detail['model_calls'][-12:]:
            path = Path(call.get('raw_path','')).resolve()
            allowed = (Path(home).resolve()/'workers').resolve()
            if allowed not in path.parents or path.name != 'result.json' or not path.is_file(): continue
            if path.stat().st_size > 2000000: continue
            response = json.loads(path.read_text(encoding='utf-8-sig'))
            request_path = path.with_name('input.json')
            request = json.loads(request_path.read_text(encoding='utf-8-sig')) if (not request_path.is_symlink()
                and allowed in request_path.resolve().parents and request_path.is_file()
                and request_path.stat().st_size < 100000) else None
            detail['worker_artifacts'].append({'request':bounded(request),'response':bounded(response.get('content')),
                                                'metrics':bounded(response.get('metric')),'error':response.get('error')})
        return redact(detail)
    finally: tickets.store.close()
