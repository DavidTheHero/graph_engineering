"""Protected operator command dispatch. Never accepts executable shell text."""
from contextlib import closing
import json
import re

from .control_repositories import register, inspect, start
from .scheduler import observe_models
from .tickets import Tickets
from .runtime import digest


def operate(home, path, data, key):
    tickets = Tickets(home)
    try:
        if path=='/api/centre/repository-policy':
            from .autonomy import configure
            return configure(tickets,data,key)
        if path == '/api/centre/create-ticket':
            from .report_store import create
            return create(tickets,data,key)
        if path == '/api/centre/intake-action':
            from .report_commands import act
            return act(tickets,data,key)
        if path == '/api/centre/register':
            if not isinstance(data.get('path'),str) or len(data['path']) > 2000: raise ValueError('Supply a repository root path')
            return register(tickets,data['path'])
        if path == '/api/centre/inspect': return inspect(tickets,data['repository_id'])
        if path == '/api/centre/start': return start(tickets,data['repository_id'],data,key)
        if path == '/api/centre/health': return observe_models(tickets,timeout=3)
        if path == '/api/centre/ticket-action':
            return tickets.command(data['ticket_id'],data['action'],data,key)
        if path == '/api/centre/scheduler':
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key): raise ValueError('Idempotency key required')
            request_hash=digest([path,data])
            tickets.db.execute('BEGIN IMMEDIATE')
            try:
                previous=tickets.db.execute('SELECT request_hash,result FROM operator_commands WHERE key=?',(key,)).fetchone()
                if previous:
                    if previous[0]!=request_hash: raise ValueError('Idempotency key reused for another command')
                    tickets.db.rollback();return json.loads(previous[1])
                if data.get('action') in ('pause','resume'):
                    setting,value='paused',data['action']=='pause'
                elif data.get('action') == 'capacity':
                    setting,value='workers',data.get('workers')
                    if type(value) is not int or not 1<=value<=8: raise ValueError('Concurrency must be 1–8')
                else: raise ValueError('Invalid scheduler command')
                tickets.db.execute('INSERT OR REPLACE INTO scheduler_settings VALUES(?,?)',(setting,json.dumps(value)))
                tickets.store._event('controller','scheduler','scheduler_'+data['action'],data)
                result={'paused':tickets.setting('paused'),'workers':tickets.setting('workers')}
                tickets.db.execute('INSERT INTO operator_commands VALUES(?,?,?)',(key,request_hash,json.dumps(result)))
                tickets.db.commit();return result
            except BaseException:
                tickets.db.rollback();raise
        raise KeyError('Unknown operator command')
    finally: tickets.store.close()
