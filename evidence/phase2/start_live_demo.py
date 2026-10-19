"""Start actual local-model demos through the protected operator API.

Generated fixture defects are intentional. GitHub publication is disabled.
The General_AI_Agent scan parses a pinned commit without executing its code.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import uuid
from urllib.request import Request, urlopen

from graph_engineering.evaluation import seed_repositories

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'evidence' / 'phase2'
URL = 'http://127.0.0.1:8770'


def source_receipt():
    repo = ROOT.parent / 'General_AI_Agent'
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])
    return {'head': git('rev-parse', 'HEAD').decode().strip(),
            'status_sha256': hashlib.sha256(git('status', '--porcelain')).hexdigest(),
            'diff_sha256': hashlib.sha256(git('diff', 'HEAD', '--binary')).hexdigest()}


def main():
    csrf = json.load(urlopen(URL + '/api/session'))['csrf']
    def post(route, data):
        return json.load(urlopen(Request(URL + '/api/centre/' + route,
            data=json.dumps(data).encode(), headers={'Origin': URL, 'X-GE-CSRF': csrf,
            'Idempotency-Key': 'phase2-' + uuid.uuid4().hex,
            'Content-Type': 'application/json'}), timeout=60))
    before = source_receipt()
    fixture_root = ROOT / '.runtime' / 'phase2-fixtures'
    if not fixture_root.exists():
        fixtures = seed_repositories(fixture_root)
    else:
        fixtures = [p for p in fixture_root.iterdir() if (p / '.git').exists()]
    state = json.load(urlopen(URL + '/api/centre'))
    general = next(r for r in state['repositories'] if r['label'].casefold() == 'general_ai_agent')
    runs = [post('start', {'repository_id': general['id'], 'mode': 'read_only',
                          'inspect_with_model': True, 'routing': 'multimodel'})]
    for fixture in fixtures:
        registered = post('register', {'path': str(fixture)})
        runs.append(post('start', {'repository_id': registered['id'], 'mode': 'assisted',
                                  'trust_execution': True, 'scan_only': False,
                                  'routing': 'multimodel'}))
    receipt = {'kind': 'live-local-model-execution', 'github_writes': False,
               'source_before': before, 'runs': runs, 'health': post('health', {})}
    (OUTPUT / 'live-start.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
