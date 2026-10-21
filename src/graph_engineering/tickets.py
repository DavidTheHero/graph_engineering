"""Additive scheduling metadata. Runs and existing events remain authoritative."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
import uuid

from .runtime import Store, digest, redact
from .scheduling_policy import retry_delay,execution_limits


QUEUES = {'high': 'Critical / High Priority', 'normal': 'Normal Priority', 'low': 'Low Priority',
          'intake':'New / Queued for Investigation','clarification':'Waiting for User Input',
          'intake_review':'Ticket Review','development':'Development Queue',
          'investigation': 'Needs Investigation', 'dependency': 'Waiting for Dependency',
          'retry': 'Retry Queue', 'approval': 'Human Approval Queue', 'dead': 'Dead Letter',
          'paused': 'Paused', 'resource': 'Waiting for Resource', 'active': 'Executing', 'done': 'History'}
QUEUES['cancelled']='Cancelled / stopped'
CATEGORIES = ('bug', 'security', 'performance', 'reliability', 'refactoring', 'test_coverage', 'technical_debt',
              'documentation', 'feature', 'build_ci', 'architecture_investigation')
TERMINAL = {'completed', 'no_findings', 'duplicate'}


class Tickets:
    def __init__(self, home: Path):
        self.store = Store(home)
        self.db = self.store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS tickets(
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
            category TEXT NOT NULL, labels TEXT NOT NULL, priority TEXT NOT NULL,
            severity TEXT NOT NULL, confidence REAL NOT NULL, dependencies TEXT NOT NULL,
            paused INTEGER DEFAULT 0, retries INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
            not_before REAL DEFAULT 0, lease_owner TEXT, lease_pid INTEGER,
            lease_model TEXT, lease_started REAL, created REAL NOT NULL, updated REAL NOT NULL,
            last_reason TEXT DEFAULT '', fingerprint TEXT UNIQUE,triaged INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS scheduler_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          INSERT OR IGNORE INTO scheduler_settings VALUES('paused','false');
          INSERT OR IGNORE INTO scheduler_settings VALUES('workers','2');
          INSERT OR IGNORE INTO scheduler_settings VALUES('model_capacity','{"a":1,"b":1}');
          CREATE TABLE IF NOT EXISTS operator_commands(
            key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS registered_repositories(
            id TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE, config_path TEXT NOT NULL, label TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS model_observations(
            key TEXT PRIMARY KEY, observed REAL NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS tickets_ready ON tickets(not_before,created);
          CREATE INDEX IF NOT EXISTS run_events ON events(run_id,seq);
        ''')
        if 'triaged' not in {r['name'] for r in self.db.execute('PRAGMA table_info(tickets)')}:
            self.db.execute('ALTER TABLE tickets ADD COLUMN triaged INTEGER DEFAULT 0')
            self.db.commit()
        from .report_store import migrate
        migrate(self.db)

    def _decode(self, row):
        value = dict(row)
        for key in ('labels', 'dependencies', 'state'):
            if key in value: value[key] = json.loads(value[key])
        return value

    def list(self):
        return [self._decode(r) for r in self.db.execute('''SELECT t.*,r.repo,r.node,r.status,r.state,
            r.created AS run_created,r.updated AS run_updated,r.cancel FROM tickets t
            JOIN runs r ON r.id=t.run_id ORDER BY t.created''')]

    def get(self, ticket_id):
        value = next((t for t in self.list() if t['id'] == ticket_id), None)
        if value is None: raise KeyError('Unknown ticket')
        return value

    def admit(self, run_id, *, priority='P2', dependencies=None, category='architecture_investigation',
              fingerprint=None, confidence=1.0, severity='medium', labels=None):
        run = self.store.get(run_id)
        existing = self.db.execute('SELECT id FROM tickets WHERE run_id=? OR fingerprint=?', (run_id, fingerprint)).fetchone()
        if existing: return self.get(existing[0])
        if category not in CATEGORIES or priority not in ('P0', 'P1', 'P2', 'P3'):
            raise ValueError('Invalid category or priority')
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError('Confidence must be between zero and one')
        dependencies = dependencies or []
        if not isinstance(dependencies, list) or len(dependencies) > 50 or any(not isinstance(x, str) for x in dependencies):
            raise ValueError('Invalid dependencies')
        known = {t['id'] for t in self.list()}
        if not set(dependencies) <= known: raise ValueError('Unknown dependency')
        ticket_id = 'GE-' + uuid.uuid4().hex[:8].upper()
        clock = time.time()
        with self.db:
            self.db.execute('''INSERT INTO tickets(id,run_id,category,labels,priority,severity,confidence,
                dependencies,created,updated,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (ticket_id, run_id, category, json.dumps((labels or [])[:20]), priority, severity,
                 confidence, json.dumps(dependencies), clock, clock, fingerprint))
            self.store._event(run_id, 'queue_admission', 'ticket_created', {'ticket_id': ticket_id,
                'priority': priority, 'category': category, 'destination': 'queue:' + ('high' if priority in ('P0','P1') else 'normal')})
        return self.get(ticket_id)

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM scheduler_settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO scheduler_settings VALUES(?,?)', (key, json.dumps(value)))

    def classify(self, ticket):
        if ticket.get('triaged'): return
        candidate = ticket['state'].get('candidate')
        if not candidate or not ticket['state'].get('validation'): return
        category = candidate.get('category', 'bug')
        category = {'logic': 'bug', 'correctness': 'bug', 'runtime': 'bug', 'build': 'build_ci', 'test': 'test_coverage'}.get(category, category)
        if category not in CATEGORIES: category = 'bug'
        severity = candidate.get('severity', 'medium')
        priority = {'critical':'P0','high':'P1','medium':'P2','low':'P3'}.get(severity, 'P2')
        changed = (category, priority, severity) != (ticket['category'], ticket['priority'], ticket['severity'])
        if changed:
            with self.db:
                self.db.execute('UPDATE tickets SET category=?,priority=?,severity=?,confidence=?,updated=? WHERE id=?',
                    (category, priority, severity, candidate.get('confidence', 0), time.time(), ticket['id']))
                self.store._event(ticket['run_id'], 'classification', 'ticket_classified',
                    {'ticket_id':ticket['id'], 'category':category, 'priority':priority, 'severity':severity})

    def queue(self, ticket, all_tickets=None, current=None):
        current = time.time() if current is None else current
        status = ticket['status']
        if status in TERMINAL: return 'done', 'Execution completed; retained in history'
        if ticket['lease_owner']: return 'active', 'Worker owns a durable execution lease'
        if ticket.get('cancel') or status=='cancelled': return 'cancelled', 'Cancelled; no further work will be dispatched. Retry explicitly to restart.'
        if status in ('failed', 'escalated') or status in ('blocked','cancelled') and ticket['retries'] >= ticket['max_retries']:
            limits=execution_limits(ticket)
            return 'dead', ('Ticket retry limit reached. ' if ticket['retries']>=ticket['max_retries'] else 'Execution escalated. ')+limits['explanation']
        if ticket['paused'] or self.setting('paused', False): return 'paused', 'Admission paused; active steps finish at their checkpoint'
        if status=='ready_for_delivery':return 'approval','Implementation and independent review passed; approve issue/PR publication to continue'
        if ticket['state']['config'].get('run_mode')=='intake':
            if status=='needs_information':return 'clarification','Investigation needs evidence or clarification; the original report is preserved'
            if status=='duplicate_review':return 'intake_review','Possible duplicate requires explicit review; no ticket was discarded'
            if status=='ready_for_review':return 'intake_review','Review the enhanced revision; source references do not prove runtime causality'
            if status in ('ready_for_development','development_queued'):return 'development','Accepted ticket; implementation has not been authorised'
            if status=='enhancement_rejected':return 'investigation','Enhancement rejected; edit or request another investigation'
            if status=='ready' and ticket['node']=='manual_intake':return 'intake','Original report saved; queued for investigation'
        if status == 'waiting_approval' and self.store.approved_head(ticket['run_id']) != ticket['state'].get('head'):
            return 'approval', 'Exact tested commit needs human approval'
        error = ticket['state'].get('error')
        if status == 'blocked' and isinstance(error, dict) and error.get('category') == 'infrastructure_exhausted':
            return 'dead', (f"Infrastructure retries exhausted ({error.get('infrastructure_limit')}) at "
                            f"{ticket['node'].replace('_',' ')}: {error.get('message')}. Check model servers/OpenCode, then Retry.")
        if status in ('blocked', 'cancelled'):
            limits=execution_limits(ticket)
            return 'investigation', limits['explanation'] if limits['exhausted'] else str(ticket['state'].get('error') or status)
        if status == 'scanned':
            if ticket['state']['config'].get('run_mode') == 'metadata':
                return 'investigation', 'Read-only observations have verified source references; they are not confirmed bugs'
            return 'approval', 'Scan complete; explicitly enable development to continue'
        if ticket['not_before'] > current:
            last = ticket['state'].get('last_error')
            when = time.strftime('%H:%M:%S', time.localtime(ticket['not_before']))
            if isinstance(last, dict) and last.get('category') == 'infrastructure':
                return 'retry', (f"Automatic infrastructure retry {last.get('infrastructure_failures')}/{last.get('infrastructure_limit')} "
                                 f"at {when} ({last.get('phase') or 'worker'}): {last.get('message')}")
            return 'retry', f"Backoff until {when}"
        all_tickets = all_tickets or {t['id']:t for t in self.list()}
        if any(all_tickets.get(dep, {}).get('status') not in TERMINAL for dep in ticket['dependencies']):
            return 'dependency', 'Waiting for all dependency tickets to finish successfully'
        if ticket['confidence'] < .8: return 'investigation', 'Low confidence requires supervised investigation'
        return ('high' if ticket['priority'] in ('P0','P1') else 'low' if ticket['priority'] == 'P3' else 'normal'), 'Eligible for dispatch'

    def command(self, ticket_id, action, data, key):
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key): raise ValueError('An idempotency key is required')
        request_hash = digest([ticket_id, action, data])
        self.db.execute('BEGIN IMMEDIATE')
        try:
            previous = self.db.execute('SELECT request_hash,result FROM operator_commands WHERE key=?', (key,)).fetchone()
            if previous:
                if previous[0] != request_hash: raise ValueError('Idempotency key was used for another command')
                self.db.rollback(); return json.loads(previous[1])
            ticket = self.get(ticket_id)
            run_id = ticket['run_id']
            updates = {}
            if action in ('pause', 'resume'):
                if action=='resume' and (ticket.get('cancel') or ticket['status']=='cancelled'):raise ValueError('Cancelled tickets require explicit Retry with backoff')
                updates['paused'] = action == 'pause'
            elif action == 'cancel':
                self.db.execute('UPDATE runs SET cancel=1 WHERE id=?', (run_id,))
                if not ticket['lease_owner']:
                    self.db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run_id,))
                updates['paused'] = True
            elif action == 'retry':
                if ticket['lease_owner']: raise ValueError('An active worker cannot be retried')
                if ticket['status'] not in ('blocked','cancelled','escalated','failed'): raise ValueError('Only unresolved executions may be retried')
                if ticket['retries'] >= ticket['max_retries']: raise ValueError('Retry budget exhausted')
                state = ticket['state']; previous_error = state.pop('error', None); state.pop('infrastructure_failures', None)
                previous_message = str(previous_error.get('message', '') if isinstance(previous_error, dict) else previous_error or '')
                state.setdefault('attempts', {})[f"{ticket['node']}:{state.get('repairs',0)}"] = 0
                if state['config'].get('run_mode')=='intake':
                    state['attempts']={key:0 if key.startswith(ticket['node']+':') else count for key,count in state['attempts'].items()}
                retry_node=ticket['node']
                if retry_node=='regression_review' and not state.get('regression_review',{}).get('approved'):
                    retry_node='regression_authoring'
                    state['regression_review_round']=0
                    state['attempts'][f"{retry_node}:{state.get('repairs',0)}"] = 0
                elif retry_node=='done' and ticket['status']=='escalated' and state['config'].get('run_mode')=='accepted_development' and state.get('regression_baseline'):
                    # An exhausted repair budget is retried by planning again from the validated regression, with a fresh budget.
                    retry_node='task_planning'
                    state.update(repairs=0,development_round=state.get('development_round',0)+1)
                    state.pop('outcome',None)
                    state['attempts']={key:0 if key.split(':')[0] in ('task_planning','model_selection','isolated_implementation','test_execution','repair','independent_review') else count
                                       for key,count in state['attempts'].items()}
                elif retry_node=='regression_validation':
                    # Re-running a rejected test cannot succeed. Start again from a pristine snapshot (development_setup
                    # discards the old checkout, including an installed or leftover proposal, and rebuilds the evidence);
                    # the rejection feedback is kept for the next author.
                    retry_node='development_setup'
                    state.update(regression_review_round=0,regression_validation_round=0)
                    state.pop('regression',None);state.pop('regression_review',None)
                    state['attempts']={key:0 if key.split(':')[0] in ('development_setup','regression_authoring','regression_review','regression_validation') else count
                                       for key,count in state['attempts'].items()}
                self.db.execute('UPDATE runs SET node=?,status=?,cancel=0,state=? WHERE id=?',
                                (retry_node,'ready',json.dumps(state),run_id))
                updates = {'retries':ticket['retries']+1, 'paused':False,
                           'not_before':time.time()+retry_delay(ticket['retries']+1)}
            elif action == 'approve':
                head, actor = data.get('head'), data.get('actor','').strip()
                if ticket['status'] != 'waiting_approval' or not isinstance(head, str) or not re.fullmatch(r'[a-fA-F0-9]{40,64}',head) or head != ticket['state'].get('head') or not actor:
                    raise ValueError('Approval requires the exact tested head and a human actor')
                self.db.execute('INSERT OR REPLACE INTO approvals VALUES(?,?,?,?)', (run_id,head,actor,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))
            elif action == 'reject':
                updates['paused'] = True
                self.db.execute('DELETE FROM approvals WHERE run_id=?', (run_id,))
            elif action == 'develop':
                state = ticket['state']
                if ticket['status'] != 'scanned' or state['config'].get('run_mode') != 'scan' or data.get('trust_execution') is not True:
                    raise ValueError('Only an approved test-based scan may continue; metadata scans need a new assisted run')
                state['config']['run_mode'] = 'development'
                self.db.execute("UPDATE runs SET state=?,status='ready' WHERE id=?", (json.dumps(state),run_id))
                updates['paused'] = False
            elif action == 'edit':
                priority, category, labels = data.get('priority',ticket['priority']),data.get('category',ticket['category']),data.get('labels',ticket['labels'])
                if priority not in ('P0','P1','P2','P3') or category not in CATEGORIES or not isinstance(labels,list) or len(labels)>20 or any(not isinstance(x,str) or len(x)>50 for x in labels):
                    raise ValueError('Invalid priority, category or labels')
                updates.update(priority=priority,category=category,labels=json.dumps(labels),triaged=True)
            elif action == 'reassign':
                if ticket['lease_owner']: raise ValueError('Pause and wait for the active step before reassignment')
                role, model = data.get('role'), data.get('model')
                from . import routing
                state = ticket['state']; models = state['config'].get('models', {})
                if (role not in ('classification','root_investigation','investigation','planning','implementation','review')
                        or model not in models or not models[model].get('model_id')):
                    raise ValueError('Invalid role/model assignment')
                if role in routing.COMPLEX_ROLES and routing.tier_of(models, model) != routing.LARGE:
                    raise ValueError('Complex engineering roles require a large-tier model')
                state['config'].setdefault('model_overrides',{})[role] = model
                self.db.execute('UPDATE runs SET state=? WHERE id=?', (json.dumps(state),run_id))
            else: raise ValueError('Unsupported ticket command')
            for field, value in updates.items():
                self.db.execute(f'UPDATE tickets SET {field}=?,updated=? WHERE id=?', (value,time.time(),ticket_id))
            result = {'ticket_id':ticket_id, 'action':action, 'accepted':True}
            event_data=data|{'ticket_id':ticket_id}
            if action=='retry':event_data|={'source':ticket['node'],'destination':retry_node}
            self.store._event(run_id, ticket['node'], 'operator_' + action, redact(event_data))
            self.db.execute('INSERT INTO operator_commands VALUES(?,?,?)', (key,request_hash,json.dumps(result)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback(); raise
