"""Допускай только ручной main workflow и выпуск с успешным доверенным staging."""

import hashlib
import json
import os
from pathlib import Path
import re

from contracts.canonical import canonical_bytes, parse_request
from infra import release_gate as gate, staging_target, production_target
from infra.object_store import ImmutableStore
from infra.staging_migrations import read_manifest
from infra.staging_app import render_gateway, load_payload, version_tag


WORKFLOW = '.github/workflows/deploy-production.yml'


def github(path):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Gym-promotion'}
    if os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    return gate.request_json('https://api.github.com/repos/' + gate.REPOSITORY + '/' + path, headers=headers)


def identity(release):
    match = re.fullmatch(r'([a-f0-9]{40})_([1-9][0-9]{0,15})_([1-9][0-9]{0,4})', release)
    if not match:
        raise ValueError('Укажи точный release ID')
    sha, run, attempt = match.groups()
    if int(run) > 9_007_199_254_740_991 or int(attempt) > 10000:
        raise ValueError('Некорректные run/attempt выпуска')
    return sha, int(run), int(attempt)


def check_staging_run(release):
    sha, run_id, attempt = identity(release)
    run = github(f'actions/runs/{run_id}/attempts/{attempt}')
    workflow = github('actions/workflows/deploy-staging.yml')
    if (run.get('id') != run_id or run.get('run_attempt') != attempt
            or run.get('head_sha') != sha or run.get('head_branch') != 'main'
            or run.get('event') != 'workflow_run' or run.get('path') != gate.WORKFLOW
            or run.get('workflow_id') != workflow.get('id')
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('repository', {}).get('id') != gate.REPOSITORY_ID
            or run.get('repository', {}).get('owner', {}).get('id') != gate.OWNER_ID
            or run.get('head_repository', {}).get('id') != gate.REPOSITORY_ID):
        raise ValueError('Не подтверждён успешный доверенный staging этого выпуска')
    result = github(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    jobs = result.get('jobs', [])
    if result.get('total_count', len(jobs)) > 100:
        raise ValueError('Неполный список заданий staging')
    successful = {j['name'] for j in jobs if j.get('status') == 'completed' and j.get('conclusion') == 'success'}
    if not {'gate', 'build', 'publish', 'migrations', 'deploy'} <= successful:
        raise ValueError('Staging не выполнил весь цикл публикации и deploy')
    return sha


def staged_release(token, release):
    sha = check_staging_run(release)
    store = ImmutableStore(token, staging_target.WEB_BUCKET)
    with store._open('GET', 'deployments/' + release + '/result.json') as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Слишком большая staging-квитанция')
    receipt = parse_request(raw)
    if (raw != canonical_bytes(receipt) or receipt.get('format') != 1
            or receipt.get('release_id') != release or receipt.get('main_sha') != sha
            or receipt.get('environment') != 'staging' or receipt.get('purpose') != 'staging-release'
            or receipt.get('smoke') != 'succeeded' or receipt.get('origin') != staging_target.ORIGIN
            or receipt.get('function_tag') != version_tag(release)):
        raise ValueError('Квитанция не подтверждает обычный staging-выпуск')
    manifest = read_manifest(token, release, receipt['manifest_sha256'])
    files = load_payload(token, manifest)
    expected = render_gateway(manifest, files['gateway.json'])
    if receipt.get('gateway_sha256') != hashlib.sha256(canonical_bytes(expected)).hexdigest():
        raise ValueError('Staging проверил другую конфигурацию Gateway')
    return manifest, receipt


def validate_dispatch(env, event, run, workflow, main_sha):
    sha = env.get('GITHUB_SHA', '')
    expected = {'GITHUB_REPOSITORY': gate.REPOSITORY, 'GITHUB_REPOSITORY_ID': str(gate.REPOSITORY_ID),
        'GITHUB_REPOSITORY_OWNER_ID': str(gate.OWNER_ID), 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_WORKFLOW_SHA': sha,
        'GITHUB_WORKFLOW_REF': gate.REPOSITORY + '/' + WORKFLOW + '@refs/heads/main'}
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or main_sha != sha or any(env.get(k) != v for k,v in expected.items()):
        raise ValueError('Production запускается только доверенным dispatch актуального main')
    run_id, attempt = int(env['GITHUB_RUN_ID']), int(env['GITHUB_RUN_ATTEMPT'])
    if (run_id < 1 or not 1 <= attempt <= 10000 or run.get('id') != run_id
            or run.get('run_attempt') != attempt or run.get('head_sha') != sha
            or run.get('head_branch') != 'main' or run.get('event') != 'workflow_dispatch'
            or run.get('path') != WORKFLOW or run.get('workflow_id') != workflow.get('id')
            or workflow.get('path') != WORKFLOW or workflow.get('state') != 'active'
            or run.get('repository', {}).get('id') != gate.REPOSITORY_ID
            or run.get('repository', {}).get('owner', {}).get('id') != gate.OWNER_ID
            or run.get('head_repository', {}).get('id') != gate.REPOSITORY_ID
            or event.get('repository', {}).get('id') != gate.REPOSITORY_ID):
        raise ValueError('Контекст production run не подтверждён')
    release = event.get('inputs', {}).get('release_id', '')
    operation = event.get('inputs', {}).get('operation', '')
    identity(release)
    if operation not in {'promote', 'rollback'}:
        raise ValueError('Неизвестная операция production')
    return {'source_sha': sha, 'run_id': run_id, 'attempt': attempt,
        'release_id': release, 'operation': operation}


def context_from_dispatch():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    return validate_dispatch(os.environ, event, github('actions/runs/' + os.environ['GITHUB_RUN_ID']),
        github('actions/workflows/deploy-production.yml'), github('git/ref/heads/main')['object']['sha'])


def validate_claims(claims, context):
    expected = {'iss': 'https://token.actions.githubusercontent.com', 'aud': gate.AUDIENCE,
        'sub': 'repo:boris-shurygin@8566203/gym@1424201451:environment:production',
        'environment': 'production', 'repository': gate.REPOSITORY, 'repository_id': str(gate.REPOSITORY_ID),
        'repository_owner_id': str(gate.OWNER_ID), 'ref': 'refs/heads/main', 'event_name': 'workflow_dispatch',
        'workflow_ref': gate.REPOSITORY + '/' + WORKFLOW + '@refs/heads/main',
        'sha': context['source_sha'], 'workflow_sha': context['source_sha'],
        'run_id': str(context['run_id']), 'run_attempt': str(context['attempt'])}
    if any(claims.get(k) != v for k,v in expected.items()):
        raise ValueError('OIDC не соответствует проверенному production dispatch')


def production_token(context):
    return gate.exchange_token(production_target.CI, lambda claims: validate_claims(claims, context))


def main():
    context = context_from_dispatch()
    check_staging_run(context['release_id'])
    print(json.dumps(context))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Допуск production отклонён: ' + type(error).__name__)
        raise SystemExit(1)
