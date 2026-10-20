"""Single-host, restartable multi-process dispatch of existing graph nodes."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import routing
from .models import ModelClient, load_models
from .runtime import BusyError, RepoLock, now
from .scheduling_policy import effective_rank, retry_delay
from .tickets import Tickets
from .workflow import Workflow


# Graph node -> (worker role, requested tier). Concrete models come from routing.select.
ROLES = {'issue_detection':('investigation',routing.SMALL), 'task_planning':('planning',routing.LARGE),
         'request_triage':('classification',routing.SMALL),'problem_investigation':('root_investigation',routing.LARGE),
         'question_answering':('question_answering',routing.SMALL),
         'regression_authoring':('regression_test',routing.LARGE),'regression_review':('regression_review',routing.SMALL),
         'repository_investigation':('investigation',routing.SMALL),
         'isolated_implementation':('implementation',routing.LARGE), 'independent_review':('review',routing.SMALL)}


def required_model(ticket, health=None):
    """The model this ticket's next step will use, or raise RoutingError when none may run it."""
    role = ROLES.get(ticket['node'])
    if not role: return None
    name, tier = role
    if ticket['node']=='regression_review':
        from .accepted_development import regression_review_model
        tier=regression_review_model(ticket['state'])
    return routing.select(ticket['state']['config'], name, tier, health)[0]


def resource_key(ticket):
    config = ticket['state']['config']
    return str(Path(config.get('source_repo', ticket['repo'])).resolve()).casefold()


def observe_models(tickets, timeout=2):
    observations = {}
    for key, config in load_models(tickets.store.root).items():
        result = ModelClient(config).health(retries=0, timeout=timeout)
        observations[key] = result
        with tickets.db:
            tickets.db.execute('INSERT OR REPLACE INTO model_observations VALUES(?,?,?)',
                               (key,time.time(),json.dumps(result)))
    return observations


def claim(tickets: Tickets, workers=2):
    db = tickets.db
    db.execute('BEGIN IMMEDIATE')
    try:
        rows = tickets.list(); lookup = {t['id']:t for t in rows}
        active = [t for t in rows if t['lease_owner']]
        if len(active) >= workers or tickets.setting('paused', False):
            db.rollback(); return None
        capacity = tickets.setting('model_capacity', {'a':1,'b':1})
        eligible = [t for t in rows if not t.get('cancel') and tickets.queue(t,lookup)[0] in ('high','normal','low','intake')]
        eligible.sort(key=lambda t:(effective_rank(t['priority'],t['created'],time.time()),t['created'],t['id']))
        health = routing.observed_health(db)
        for ticket in eligible:
            # routing.select already skips models observed unavailable; it raises when none remain.
            try:
                model, reason = required_model(ticket, health), ''
            except routing.RoutingError as exc:
                model, reason = None, str(exc) + '; awaiting health recovery or routing change'
            if not reason and any(resource_key(a) == resource_key(ticket) for a in active):
                reason = 'Repository is owned by another active ticket'
            elif not reason and model and sum(a['lease_model'] == model for a in active) >= capacity.get(model,1):
                reason = f'Model {model.upper()} capacity is occupied'
            if reason:
                db.execute('UPDATE tickets SET last_reason=? WHERE id=?', (reason,ticket['id']))
                continue
            owner = uuid.uuid4().hex
            source_queue = tickets.queue(ticket,lookup)[0]
            db.execute('''UPDATE tickets SET lease_owner=?,lease_model=?,lease_started=?,last_reason='',updated=?
                          WHERE id=? AND lease_owner IS NULL''', (owner,model,time.time(),time.time(),ticket['id']))
            tickets.store._event(ticket['run_id'], 'queue:' + source_queue, 'queue_dispatched',
                {'ticket_id':ticket['id'],'source':'queue:' + source_queue,'destination':ticket['node'],
                 'execution_attempt_id':owner,'model_key':model})
            db.commit()
            return tickets.get(ticket['id'])
        db.commit(); return None
    except BaseException:
        db.rollback(); raise


def recover(tickets, protected_pids=()):
    """Kernel worker locks, not a wall-clock lease expiry, decide ownership."""
    for ticket in tickets.list():
        if not ticket['lease_owner']: continue
        if ticket['lease_pid'] in protected_pids: continue
        try:
            with RepoLock(tickets.store.root, 'ticket:' + ticket['id']):
                with tickets.db:
                    # Fence a worker that was launched but had not acquired its lock.
                    tickets.db.execute('''UPDATE tickets SET lease_owner=NULL,lease_pid=NULL,lease_model=NULL,
                        not_before=?,last_reason=? WHERE id=?''',
                        (time.time()+2,'Controller/worker interruption reconciled',ticket['id']))
                    if ticket['status'] == 'running':
                        tickets.db.execute("UPDATE runs SET status='ready' WHERE id=?", (ticket['run_id'],))
                    tickets.store._event(ticket['run_id'],ticket['node'],'worker_recovered',
                        {'ticket_id':ticket['id'],'execution_attempt_id':ticket['lease_owner']})
        except BusyError:
            pass


def execute_assignment(home, ticket_id, owner):
    with RepoLock(home, 'ticket:' + ticket_id):
        # Reuse exactly one Store/connection per process, never across worker threads.
        tickets = Tickets(home)
        try:
            ticket = tickets.get(ticket_id)
            if ticket['lease_owner'] != owner: return 0
            workflow = Workflow(home)
            try:
                # Read-only/assisted scans yield before implementation planning.
                until = 'task_planning' if ticket['state']['config'].get('run_mode') in ('metadata','scan') else None
                workflow.execute(ticket['run_id'], until=until, single_step=True)
            finally:
                workflow.store.close()
            current = tickets.get(ticket_id)
            last = tickets.db.execute('SELECT kind FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1', (ticket['run_id'],)).fetchone()
            tickets.classify(current)
            delay = retry_delay(max(1,current['retries']+1)) if last and last[0] == 'retry_scheduled' else 0
            if last and last[0] == 'retry_backoff':
                # Infrastructure fault: the workflow chose the backoff; the ticket waits in the Retry queue.
                delay = max(0, current['state'].get('retry_not_before', 0) - time.time())
            with tickets.db:
                tickets.db.execute('''UPDATE tickets SET lease_owner=NULL,lease_pid=NULL,lease_model=NULL,
                    not_before=?,updated=? WHERE id=? AND lease_owner=?''', (time.time()+delay,time.time(),ticket_id,owner))
                queued = tickets.get(ticket_id)
                queue = tickets.queue(queued,{t['id']:t for t in tickets.list()})[0]
                tickets.store._event(ticket['run_id'],current['node'],'dispatch_checkpoint',
                    {'ticket_id':ticket_id,'execution_attempt_id':owner,'source':ticket['node'],
                     'destination':current['node'] if queue == 'done' else 'queue:'+queue,
                     'next_node':current['node'],'status':current['status']})
            return 0
        except BaseException as exc:
            with tickets.db:
                row = tickets.db.execute('SELECT run_id FROM tickets WHERE id=?', (ticket_id,)).fetchone()
                if row:
                    tickets.db.execute("UPDATE runs SET status='blocked' WHERE id=?", (row[0],))
                    tickets.store._event(row[0],'scheduler','worker_failed',
                        {'ticket_id':ticket_id,'execution_attempt_id':owner,'message':type(exc).__name__ + ': ' + str(exc)})
                tickets.db.execute('UPDATE tickets SET lease_owner=NULL,lease_pid=NULL,lease_model=NULL,last_reason=? WHERE id=? AND lease_owner=?',
                    ('Worker process failed; manual retry required',ticket_id,owner))
            return 1
        finally:
            tickets.store.close()


def serve_scheduler(home, workers=2, once=False, poll=.5):
    if not 1 <= workers <= 8: raise ValueError('Worker concurrency must be between 1 and 8')
    home = Path(home).resolve()
    children = []
    with RepoLock(home, 'scheduler'):
        tickets = Tickets(home)
        try:
            tickets.set_setting('workers',workers)
            last_health = 0
            while True:
                tickets.set_setting('worker_service',{'pid':os.getpid(),'observed':time.time()})
                children = [p for p in children if p.poll() is None]
                recover(tickets, {p.pid for p in children})
                from .autonomy import dispatch
                if not tickets.setting('paused',False):dispatch(tickets)
                if time.time() - last_health > 30:
                    observe_models(tickets); last_health = time.time()
                while True:
                    ticket = claim(tickets,tickets.setting('workers',workers))
                    if ticket is None: break
                    command = [sys.executable,'-m','graph_engineering','--home',str(home),'worker-step',
                               ticket['id'],ticket['lease_owner']]
                    kwargs = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session':True}
                    log = home / 'scheduler-workers.log'
                    try:
                        with log.open('ab') as output:
                            child = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=output,stderr=output,**kwargs)
                        children.append(child)
                        with tickets.db:
                            tickets.db.execute('UPDATE tickets SET lease_pid=? WHERE id=? AND lease_owner=?',
                                               (child.pid,ticket['id'],ticket['lease_owner']))
                    except OSError:
                        recover(tickets); raise
                if once:
                    for child in children: child.wait(timeout=650)
                    return
                time.sleep(poll)
        finally:
            tickets.store.close()
