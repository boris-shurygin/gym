"""Ожидай успешный staging и сверь публичный release ID без облачных credentials."""

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPSHandler

from infra.object_store import NoRedirect

REPO = 'boris-shurygin/gym'
ORIGIN = 'https://d5d734k3s76t42pa5rtn.764nr5vy.apigw.yandexcloud.net'
CI = '.github/workflows/ci.yml'
STAGING = '.github/workflows/deploy-staging.yml'


def state_for(runs, sha):
    def candidates(path, event):
        return [r for r in runs if r.get('head_sha') == sha and r.get('head_branch') == 'main'
            and r.get('path') == path and r.get('event') == event]
    checks = candidates(CI, 'push')
    if not checks:
        return {'state': 'waiting', 'phase': 'main-ci'}
    check = max(checks, key=lambda r: r['id'])
    if check['status'] != 'completed':
        return {'state': 'waiting', 'phase': 'main-ci'}
    if check['conclusion'] != 'success':
        return {'state': 'failed', 'phase': 'main-ci', 'run_id': check['id']}
    stages = [r for r in candidates(STAGING, 'workflow_run')
        if r.get('created_at', '') >= check.get('updated_at', '')]
    if not stages:
        return {'state': 'waiting', 'phase': 'staging'}
    stage = max(stages, key=lambda r: r['id'])
    result = {'state': 'waiting', 'phase': 'staging', 'run_id': stage['id'],
        'url': f'https://github.com/{REPO}/actions/runs/{stage["id"]}'}
    if stage['status'] == 'completed':
        result['state'] = 'ready' if stage['conclusion'] == 'success' else 'failed'
        result['release_id'] = f'{sha}_{stage["id"]}_{stage["run_attempt"]}'
    return result


def verify_identity(value, result, sha):
    if value != {'main_sha': sha, 'purpose': 'staging-release', 'release_id': result['release_id']}:
        raise ValueError('Публичный staging не соответствует успешному выпуску')


def get_json(url, *, github=False):
    headers = {'User-Agent': 'Gym-delivery-status'}
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if github and token:
        headers['Authorization'] = 'Bearer ' + token
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with build_opener(NoRedirect(), HTTPSHandler(context=context)).open(Request(url, headers=headers), timeout=20) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--interval', type=int, default=30)
    parser.add_argument('--output', default='.local/delivery-result.json')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{40}', args.sha) or not 1 <= args.timeout <= 3600 or not 15 <= args.interval <= 60:
        raise ValueError('Укажи SHA и ограниченное время ожидания')
    started, last, failures = time.monotonic(), None, 0
    while True:
        try:
            runs = get_json(f'https://api.github.com/repos/{REPO}/actions/runs?head_sha={args.sha}&per_page=30', github=True)['workflow_runs']
            result = state_for(runs, args.sha)
            if result['state'] == 'ready':
                verify_identity(get_json(ORIGIN + '/__gym/release'), result, args.sha)
                result['origin'] = ORIGIN
            failures = 0
        except HTTPError as error:
            result = {'state': 'unavailable', 'http_status': error.code}
            if error.code in {401, 403, 429}:
                break
            failures += 1
        except (OSError, ValueError) as error:
            result = {'state': 'unavailable', 'error_type': type(error).__name__}
            failures += 1
        if result != last:
            print(json.dumps(result), flush=True)
            last = result
        if result['state'] in {'ready', 'failed'} or failures >= 3 or not args.wait:
            break
        if time.monotonic() - started >= args.timeout:
            result = {'state': 'timeout', 'sha': args.sha}
            break
        time.sleep(min(args.interval, max(0, args.timeout - (time.monotonic() - started))))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    if result['state'] != 'ready':
        print(json.dumps(result), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
