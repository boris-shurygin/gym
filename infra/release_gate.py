"""Допускай публикацию только после успешного push CI того же доверенного main SHA."""

import base64
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

from infra.object_store import NoRedirect


REPOSITORY = 'boris-shurygin/gym'
REPOSITORY_ID = 1424201451
OWNER_ID = 8566203
CHECKS_ID = 396380311
WORKFLOW = '.github/workflows/deploy-staging.yml'
PUBLISHER = 'aje0omi931fri66pbdir'
AUDIENCE = 'https://github.com/boris-shurygin/gym'
SUBJECT = 'repo:boris-shurygin@8566203/gym@1424201451:ref:refs/heads/main'


def request_json(url, *, headers=None, data=None):
    request = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        return json.load(response)


def github(path):
    return request_json('https://api.github.com/repos/' + REPOSITORY + '/' + path,
        headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'User-Agent': 'Gym-release'})


def validate_context(env, event, source, current, workflow, jobs, main_sha):
    sha = source.get('head_sha', '')
    expected = {'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REPOSITORY_ID': str(REPOSITORY_ID),
                'GITHUB_REPOSITORY_OWNER_ID': str(OWNER_ID), 'GITHUB_REF': 'refs/heads/main',
                'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_WORKFLOW_REF': REPOSITORY + '/' + WORKFLOW + '@refs/heads/main',
                'GITHUB_SHA': sha, 'GITHUB_WORKFLOW_SHA': sha}
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or any(env.get(k) != v for k, v in expected.items()):
        raise ValueError('Выпуск не запущен доверенным workflow того же main SHA')
    if main_sha != sha:
        raise ValueError('CI относится к уже заменённому main; дождись CI актуального SHA')
    for run in (source, current):
        if (run.get('repository', {}).get('id') != REPOSITORY_ID
                or run.get('repository', {}).get('owner', {}).get('id') != OWNER_ID
                or run.get('head_repository', {}).get('id') != REPOSITORY_ID
                or run.get('head_sha') != sha or run.get('head_branch') != 'main'):
            raise ValueError('Run принадлежит другой ветке/репозиторию/владельцу')
    if (source.get('workflow_id') != CHECKS_ID or source.get('event') != 'push'
            or source.get('status') != 'completed' or source.get('conclusion') != 'success'
            or source.get('path') != '.github/workflows/ci.yml'
            or event.get('action') != 'completed'
            or event.get('repository', {}).get('id') != REPOSITORY_ID
            or event.get('workflow_run', {}).get('id') != source.get('id')
            or event.get('workflow_run', {}).get('head_sha') != sha
            or event.get('workflow_run', {}).get('run_attempt') != source.get('run_attempt')):
        raise ValueError('Не подтверждён успешный push CI нужной попытки')
    if (current.get('id') != int(env['GITHUB_RUN_ID']) or current.get('event') != 'workflow_run'
            or current.get('run_attempt') != int(env['GITHUB_RUN_ATTEMPT'])
            or current.get('path') != WORKFLOW or workflow.get('path') != WORKFLOW
            or current.get('workflow_id') != workflow.get('id') or workflow.get('state') != 'active'):
        raise ValueError('Текущий run не соответствует процессу публикации')
    required = {'contracts', 'api', 'ydb', 'web', 'changes'}
    successful = {job['name'] for job in jobs if job.get('status') == 'completed' and job.get('conclusion') == 'success'}
    if not required <= successful:
        raise ValueError('Обязательные проверки CI не подтверждены')
    run_id, attempt = int(env['GITHUB_RUN_ID']), int(env['GITHUB_RUN_ATTEMPT'])
    if not (0 < run_id <= 9_007_199_254_740_991 and 0 < attempt <= 10000):
        raise ValueError('Некорректные run/attempt')
    return {'source_sha': sha, 'run_id': run_id, 'attempt': attempt,
            'release_id': f'{sha}_{run_id}_{attempt}', 'checks_run_id': source['id']}


def context_from_github():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    source_id = event.get('workflow_run', {}).get('id')
    current_id = os.environ['GITHUB_RUN_ID']
    if type(source_id) is not int or source_id < 1 or not current_id.isdigit():
        raise ValueError('Некорректные идентификаторы workflow run')
    source = github('actions/runs/' + str(source_id))
    current = github('actions/runs/' + current_id)
    workflow = github('actions/workflows/deploy-staging.yml')
    jobs = github(f'actions/runs/{source_id}/attempts/{source["run_attempt"]}/jobs?per_page=100')['jobs']
    main_sha = github('git/ref/heads/main')['object']['sha']
    return validate_context(os.environ, event, source, current, workflow, jobs, main_sha)


def validate_publisher_claims(claims, context, *, staging=False):
    subject = SUBJECT if not staging else 'repo:boris-shurygin@8566203/gym@1424201451:environment:staging'
    expected = {'iss': 'https://token.actions.githubusercontent.com', 'aud': AUDIENCE, 'sub': subject,
                'repository': REPOSITORY, 'repository_id': str(REPOSITORY_ID),
                'repository_owner_id': str(OWNER_ID), 'ref': 'refs/heads/main', 'event_name': 'workflow_run',
                'workflow_ref': REPOSITORY + '/' + WORKFLOW + '@refs/heads/main',
                'sha': context['source_sha'], 'workflow_sha': context['source_sha'],
                'run_id': str(context['run_id']), 'run_attempt': str(context['attempt'])}
    if staging:
        expected['environment'] = 'staging'
    if any(claims.get(k) != v for k, v in expected.items()):
        raise ValueError('OIDC не соответствует проверенному run/source SHA')


def exchange_token(account, validate_claims):
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.actions.githubusercontent.com')
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('Некорректный endpoint GitHub OIDC')
    response = request_json(url + ('&' if parsed.query else '?') + urllib.parse.urlencode({'audience': AUDIENCE}),
        headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    jwt = response['value']
    parts = jwt.split('.')
    if len(parts) != 3:
        raise ValueError('Некорректный JWT')
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    validate_claims(claims)
    # Локальные проверки ловят ошибку контекста; подпись JWT проверяет YC/JWKS.
    result = request_json('https://auth.yandex.cloud/oauth/token',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        data=urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'audience': account,
            'subject_token': jwt, 'subject_token_type': 'urn:ietf:params:oauth:token-type:id_token'}).encode())
    return result['access_token']


def _workload_token(context, *, staging=False):
    return exchange_token('aje1ia9cf3t1g7o5p6pg' if staging else PUBLISHER,
        lambda claims: validate_publisher_claims(claims, context, staging=staging))


def publisher_token(context):
    return _workload_token(context)


def staging_token(context):
    return _workload_token(context, staging=True)


def main():
    context = context_from_github()
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        for key in ('source_sha', 'release_id'):
            output.write(key + '=' + context[key] + '\n')
    print(json.dumps(context))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Допуск выпуска отклонён: ' + type(error).__name__)
        raise SystemExit(1)
