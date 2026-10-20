"""Atomic natural-language intake and append-only engineering ticket revisions."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
import uuid

from .runtime import digest, now, redact

# Further investigations an intake ticket may request (operator clarifications and model answers alike).
INVESTIGATION_BUDGET = 3


def migrate(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS ticket_reports(
        ticket_id TEXT PRIMARY KEY REFERENCES tickets(id), repository_id TEXT NOT NULL,
        original TEXT NOT NULL, options TEXT NOT NULL, created TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ticket_revisions(
        ticket_id TEXT NOT NULL REFERENCES tickets(id), number INTEGER NOT NULL,
        revision_key TEXT NOT NULL, author TEXT NOT NULL, at TEXT NOT NULL,
        content TEXT NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(ticket_id,number), UNIQUE(ticket_id,revision_key));
    ''')


def create(tickets, data, key):
    from .control_repositories import repositories
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):
        raise ValueError('Idempotency key required')
    original=data.get('description')
    if not isinstance(original,str) or not original.strip() or len(original)>6000:
        raise ValueError('Describe the problem in 1–6000 characters')
    priority=data.get('priority') or 'P2'
    if priority not in ('P0','P1','P2','P3'):raise ValueError('Invalid priority')
    options={}
    for field in ('logs','expected','context'):
        value=data.get(field,'')
        if not isinstance(value,str) or len(value)>4000:raise ValueError('Optional text must be at most 4000 characters')
        options[field]=value
    related=data.get('related_ticket') or None
    if related: tickets.get(related)
    options.update(related_ticket=related,reproduce=data.get('reproduce') is True)
    repo=next((r for r in repositories(tickets) if r['id']==data.get('repository_id')),None)
    if repo is None:raise ValueError('Choose a registered repository')
    config=json.loads(Path(repo['config_path']).read_text(encoding='utf-8-sig'))
    if options['reproduce'] and (not config.get('trusted_local') or data.get('trust_execution') is not True):
        raise ValueError('Reproduction requires a trusted repository and explicit code-execution approval')
    config.update(repo=repo['source'],source_repo=repo['source'],run_mode='intake',auto_merge=False,
                  # Intake keeps a measured assignment when one is configured; otherwise role tiers.
                  routing='assigned' if config.get('routing')=='assigned' and config.get('role_routes') else 'multimodel',
                  intake_repository_id=repo['id'])
    request_hash=digest(['create-ticket',data])
    db=tickets.db;db.execute('BEGIN IMMEDIATE')
    try:
        previous=db.execute('SELECT request_hash,result FROM operator_commands WHERE key=?',(key,)).fetchone()
        if previous:
            if previous[0]!=request_hash:raise ValueError('Idempotency key reused for another request')
            db.rollback();return json.loads(previous[1])
        run_id=uuid.uuid4().hex[:16];ticket_id='GE-'+uuid.uuid4().hex[:8].upper();clock=now()
        state={'config':redact(config),'task_id':run_id,'attempts':{},'repairs':0,
               'intake':{'ticket_id':ticket_id,'repository_id':repo['id'],'generation':0},
               'intake_phase':'New / Queued for Investigation'}
        db.execute('INSERT INTO runs(id,repo,node,status,state,created,updated) VALUES(?,?,?,?,?,?,?)',
                   (run_id,config['repo'],'manual_intake','ready',json.dumps(state),clock,clock))
        db.execute('''INSERT INTO tickets(id,run_id,category,labels,priority,severity,confidence,
                      dependencies,created,updated,fingerprint,triaged) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (ticket_id,run_id,'architecture_investigation','[]',priority,'medium',1,'[]',time.time(),time.time(),
                    digest(['manual',run_id]),int(bool(data.get('priority')))))
        db.execute('INSERT INTO ticket_reports VALUES(?,?,?,?,?)',
                   (ticket_id,repo['id'],original,json.dumps(options),clock))
        tickets.store._event(run_id,'manual_intake','ticket_created',{'ticket_id':ticket_id,
            'source':'manual_intake','destination':'queue:intake','status':'queued'})
        result={'ticket_id':ticket_id,'run_id':run_id,'status':state['intake_phase']}
        db.execute('INSERT INTO operator_commands VALUES(?,?,?)',(key,request_hash,json.dumps(result)))
        db.commit();return result
    except BaseException:
        db.rollback();raise


def report(db,ticket_id):
    row=db.execute('SELECT * FROM ticket_reports WHERE ticket_id=?',(ticket_id,)).fetchone()
    if not row:return None
    result=dict(row);result['options']=json.loads(result['options'])
    result['revisions']=[dict(r)|{'content':json.loads(r['content']),'evidence':json.loads(r['evidence'])}
        for r in db.execute('SELECT * FROM ticket_revisions WHERE ticket_id=? ORDER BY number',(ticket_id,))]
    return result


def revision(db,ticket_id,key,content,evidence,author):
    existing=db.execute('SELECT number,content FROM ticket_revisions WHERE ticket_id=? AND revision_key=?',(ticket_id,key)).fetchone()
    if existing:return existing['number'],json.loads(existing['content'])
    number=db.execute('SELECT COALESCE(MAX(number),0)+1 FROM ticket_revisions WHERE ticket_id=?',(ticket_id,)).fetchone()[0]
    db.execute('INSERT INTO ticket_revisions VALUES(?,?,?,?,?,?,?)',
               (ticket_id,number,key,author,now(),json.dumps(content),json.dumps(evidence)))
    return number,content
