"""Operator-owned repository registration and pinned, isolated snapshot starts."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

from .models import default_models
from .repository import discover, git, source_bundle
from .routing import PROFILES as ROUTING_PROFILES
from .runtime import RepoLock, digest
from .tickets import Tickets
from .workflow import initialise


def import_configurations(tickets):
    for path in sorted((tickets.store.root / 'repositories').glob('*.json')):
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        source = str(Path(config.get('source_repo',config['repo'])).resolve())
        key = digest(source.casefold())[:16]
        with tickets.db:
            tickets.db.execute('INSERT OR IGNORE INTO registered_repositories VALUES(?,?,?,?)',
                               (key,source,str(path),Path(source).name))


def repositories(tickets):
    import_configurations(tickets)
    result = []
    for row in tickets.db.execute('SELECT * FROM registered_repositories ORDER BY label'):
        value = dict(row)
        from .autonomy import policy
        value['autonomy']=policy(tickets,row['id'])
        config = json.loads(Path(row['config_path']).read_text(encoding='utf-8-sig'))
        value.update({'trusted':config.get('trusted_local',False),'backend':config['backend'],
                      'routing':config.get('routing','baseline'),'auto_merge':config.get('auto_merge',False)})
        result.append(value)
    return result


def register(tickets, path):
    source = Path(path).resolve()
    existing = next((r for r in repositories(tickets) if Path(r['source']) == source),None)
    if existing: return existing
    config_path = initialise(source,tickets.store.root,trusted_local=False)
    key = digest(str(source).casefold())[:16]
    with tickets.db:
        tickets.db.execute('INSERT OR IGNORE INTO registered_repositories VALUES(?,?,?,?)',
                           (key,str(source),str(config_path),source.name))
    return next(r for r in repositories(tickets) if r['id'] == key)


def inspect(tickets, repo_id):
    repo = next((r for r in repositories(tickets) if r['id'] == repo_id), None)
    if repo is None: raise KeyError('Unknown registered repository')
    info = discover(Path(repo['source']))
    # Configuration evidence can include instruction text, never execute it.
    return repo | {k:info[k] for k in ('sha','base','clean','remote','adapter','test_command','build_commands','ci_files','policy_files')}


def _snapshot(tickets, source, revision):
    path = tickets.store.root / 'snapshots' / digest([str(source),revision])[:20]
    if not path.exists():
        # Local clone writes only into the controller and excludes uncommitted work.
        git(tickets.store.root,'clone','--no-hardlinks','--no-checkout',str(source),str(path))
        git(path,'checkout','-B','graph-base',revision)
    if git(path,'rev-parse','HEAD') != revision: raise ValueError('Pinned snapshot revision changed')
    if git(path,'status','--porcelain'): raise ValueError('Snapshot contains unexpected changes')
    return path


def start(tickets, repo_id, data, key):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key): raise ValueError('Idempotency key required')
    mode = data.get('mode','read_only')
    if mode not in ('read_only','assisted','autonomous'): raise ValueError('Invalid operation mode')
    repo = next((r for r in repositories(tickets) if r['id'] == repo_id), None)
    if repo is None: raise KeyError('Unknown registered repository')
    request_hash = digest(['start',repo_id,data])
    with RepoLock(tickets.store.root,'start-repository:'+repo_id):
        previous = tickets.db.execute('SELECT request_hash,result FROM operator_commands WHERE key=?',(key,)).fetchone()
        if previous:
            if previous[0] != request_hash: raise ValueError('Idempotency key reused for another request')
            return json.loads(previous[1])
        if mode != 'read_only' and data.get('trust_execution') is not True:
            raise ValueError('Development requires explicit trust of repository code execution')
        dependencies = data.get('dependencies',[])
        if (data.get('priority','P2') not in ('P0','P1','P2','P3') or not isinstance(dependencies,list)
            or len(dependencies)>50 or any(not isinstance(dep,str) for dep in dependencies)
            or not set(dependencies)<={t['id'] for t in tickets.list()}):
            raise ValueError('Invalid priority or dependency tickets')
        if data.get('routing','multimodel') not in ROUTING_PROFILES:
            raise ValueError('Invalid model routing')
        config = json.loads(Path(repo['config_path']).read_text(encoding='utf-8-sig'))
        if mode == 'autonomous' and not (config.get('auto_merge') and config.get('auto_merge_paths')):
            raise ValueError('Autonomous mode requires an existing explicitly scoped automatic merge policy')
        revision = git(Path(repo['source']),'rev-parse','HEAD')
        fingerprint = digest([repo_id,revision,mode,data.get('scan_only',False),
                              data.get('inspect_with_model',False) if mode == 'read_only' else False])
        existing = tickets.db.execute('SELECT id,run_id FROM tickets WHERE fingerprint=?',(fingerprint,)).fetchone()
        if existing:
            result = {'run_id':existing['run_id'],'ticket_id':existing['id'],'mode':mode,
                      'revision':revision,'source_repo':repo['source']}
            with tickets.db:
                tickets.db.execute('INSERT INTO operator_commands VALUES(?,?,?)',(key,request_hash,json.dumps(result)))
            return result
        run_id = digest([repo_id,key])[:16]
        try:
            run = tickets.store.get(run_id)
        except KeyError:
            path = _snapshot(tickets,Path(repo['source']),revision)
            config.update(repo=str(path),source_repo=repo['source'],base='graph-base',
                          run_mode='metadata' if mode == 'read_only' else 'scan' if data.get('scan_only',False) else 'development',
                          trusted_local=mode != 'read_only',auto_merge=mode == 'autonomous',
                          routing=data.get('routing',config.get('routing','multimodel')))
            config['inspect_with_model'] = mode == 'read_only' and data.get('inspect_with_model',False) is True
            config['parallel_quality'] = mode != 'read_only' and data.get('parallel_quality',False) is True
            if config['routing'] not in ROUTING_PROFILES: raise ValueError('Invalid model routing')
            if config['routing']=='assigned' and not config.get('role_routes'):
                raise ValueError('Assigned routing needs role_routes; run graph-engineer models assign --apply')
            # Read-only scans never publish. GitHub development uses the explicitly
            # configured remote, never the local clone source as a push destination.
            if mode == 'read_only': config['backend'] = 'local'
            if config['backend'] == 'github':
                remote = git(Path(repo['source']),'remote','get-url','origin')
                if not remote.startswith('https://github.com/'): raise ValueError('Live publication requires an approved HTTPS GitHub remote')
                git(path,'remote','set-url','origin',remote)
                config['base'] = json.loads(Path(repo['config_path']).read_text(encoding='utf-8-sig'))['base']
                git(path,'branch','-f',config['base'],revision)
            run = tickets.store.create_run(config,run_id)
        ticket = tickets.admit(run['id'],priority=data.get('priority','P2'),
                              dependencies=data.get('dependencies',[]),fingerprint=fingerprint)
        result = {'run_id':ticket['run_id'],'ticket_id':ticket['id'],'mode':mode,'revision':revision,'source_repo':repo['source']}
        with tickets.db:
            tickets.db.execute('INSERT INTO operator_commands VALUES(?,?,?)',(key,request_hash,json.dumps(result)))
        return result


def metadata_check(repo, info):
    """Parse committed source without importing/executing any repository code."""
    started = time.monotonic()
    findings = []
    checked = 0
    # Skip Python syntax checks if the repository declares a newer interpreter.
    manifest = Path(repo) / 'pyproject.toml'
    minimum = re.search(r'requires-python\s*=\s*[\"\']>=?(\d+)\.(\d+)',manifest.read_text(encoding='utf-8') if manifest.exists() else '')
    python_supported = not minimum or tuple(map(int,minimum.groups())) <= sys.version_info[:2]
    for name in info['source_files'][:500]:
        path = Path(repo) / name
        if path.is_symlink() or path.stat().st_size > 100000: continue
        text = path.read_text(encoding='utf-8',errors='replace'); checked += 1
        match = re.search(r'^(<<<<<<< |>>>>>>> )',text,re.M)
        if match: findings.append({'file':name,'line':text[:match.start()].count('\n')+1,'message':'Unresolved merge conflict marker'})
        if name.endswith('.py') and python_supported:
            try: ast.parse(text,filename=name)
            except SyntaxError as exc: findings.append({'file':name,'line':exc.lineno,'message':exc.msg})
    return {'argv':['internal','read-only-source-parser'],'returncode':1 if findings else 0,
            'output': '\n'.join(f"Error: {f['file']}:{f['line']}: {f['message']}" for f in findings) or f'Parsed metadata/source for {checked} files. No supported parser finding. Repository code was not executed.',
            'passed':not findings,'timed_out':False,'empty_suite':False,'duration_seconds':time.monotonic()-started,
            'check_type':'metadata_only','findings':findings,'python_parser_supported':python_supported}
