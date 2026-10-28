"""Проверь опубликованный выпуск и выполни ограниченные миграции внутри staging."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from contracts.canonical import canonical_bytes, parse_request
from infra.object_store import ImmutableStore, NoRedirect
from infra.release_artifacts import check_manifest
from infra.release_gate import context_from_github, staging_token
from infra import staging_target as target


CONTROL = 'https://serverless-containers.api.cloud.yandex.net'
OPERATIONS = 'https://operation.api.cloud.yandex.net'


class InterventionRequired(RuntimeError):
    """Не вытесняй lease и не скрывай неоднозначное состояние повтором."""


class Cloud:
    def __init__(self, token):
        self.token = token

    def json(self, url, payload=None):
        if not any(url.startswith(host + '/') for host in (CONTROL, OPERATIONS, target.URL)):
            raise ValueError('Endpoint вне согласованного staging')
        request = Request(url, data=canonical_bytes(payload) if payload is not None else None,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=135 if url.startswith(target.URL + '/') else 30) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Слишком большой административный ответ')
        return parse_request(raw)

    def wait(self, operation):
        deadline = time.monotonic() + 180
        while not operation.get('done') and time.monotonic() < deadline:
            identifier = operation.get('id', '')
            if not re.fullmatch(r'[a-z0-9]{1,64}', identifier):
                raise ValueError('Некорректная операция развёртывания')
            time.sleep(2)
            operation = self.json(OPERATIONS + '/operations/' + identifier)
        if not operation.get('done') or operation.get('error'):
            raise InterventionRequired('Развёртывание не подтверждено; проверь реальную ревизию перед повтором')


def read_manifest(token, release_id, expected_digest):
    if (not re.fullmatch(r'[a-f0-9]{40}_[1-9][0-9]{0,15}_[1-9][0-9]{0,4}', release_id)
            or not re.fullmatch(r'[a-f0-9]{64}', expected_digest)):
        raise ValueError('Укажи точный release ID и SHA-256 манифеста publisher')
    store = ImmutableStore(token, target.DELIVERY)
    with store._open('GET', 'releases/' + release_id + '/manifest.json') as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('Манифест отличается от подтверждения publisher')
    manifest = parse_request(raw)
    check_manifest(manifest)
    if raw != canonical_bytes(manifest) or manifest['release_id'] != release_id:
        raise ValueError('Манифест не является каноническим выпуском')
    # Проверь все готовые файлы до первого изменения облачного исполнителя.
    for item in manifest['artifacts']:
        if not store.verify('blobs/sha256/' + item['sha256'], item['sha256'], item['bytes']):
            raise ValueError('Артефакт выпуска не найден')
    return manifest


def revision_payload(manifest, *, mode='worker'):
    return {'containerId': target.CONTAINER, 'description': 'Gym staging migration release ' + manifest['release_id'],
            'resources': {'memory': '536870912', 'cores': '1', 'coreFraction': '100'},
            'executionTimeout': '120s', 'concurrency': '1', 'serviceAccountId': target.MIGRATOR,
            'imageSpec': {'imageUrl': manifest['migration']['image'],
                          'environment': target.worker_environment(manifest['release_id'], mode)},
            'provisionPolicy': {'minInstances': '0'}, 'logOptions': {'disabled': True, 'folderId': target.FOLDER},
            'scalingPolicy': {'zoneInstancesLimit': '1', 'zoneRequestsLimit': '1'}, 'runtime': {'http': {}},
            'metadataOptions': {'gceHttpEndpoint': 'ENABLED', 'awsV1HttpEndpoint': 'DISABLED'}}


def matches(revision, expected):
    image = revision.get('image', {})
    return (revision.get('status') == 'ACTIVE' and revision.get('containerId') == target.CONTAINER
            and image.get('imageDigest') == expected['imageSpec']['imageUrl'].split('@')[1]
            and image.get('environment') == expected['imageSpec']['environment']
            and not any(image.get(key) for key in ('command', 'args', 'workingDir'))
            and all(revision.get(key) == expected[key] for key in ('resources', 'executionTimeout', 'serviceAccountId',
                      'metadataOptions', 'logOptions', 'runtime', 'scalingPolicy'))
            and revision.get('concurrency', '1') == '1'
            and revision.get('provisionPolicy', {}).get('minInstances', '0') == '0'
            and not any(revision.get(key) for key in ('secrets', 'connectivity', 'mounts', 'storageMounts', 'asyncInvocationConfig')))


def active_revision(cloud):
    container = cloud.json(CONTROL + '/containers/v1/containers/' + target.CONTAINER)
    if (container.get('folderId') != target.FOLDER or container.get('name') != 'gym-migrations'
            or container.get('labels', {}).get('gym-project') != 'gym'):
        raise ValueError('Контейнер не соответствует staging')
    bindings = cloud.json(CONTROL + '/containers/v1/containers/' + target.CONTAINER + ':listAccessBindings')
    if bindings.get('nextPageToken') or any(b.get('subject', {}).get('type') == 'system' for b in bindings.get('accessBindings', [])):
        raise ValueError('Приватность контейнера не подтверждена')
    revisions = cloud.json(CONTROL + '/containers/v1/revisions?containerId=' + target.CONTAINER + '&pageSize=1000')
    if revisions.get('nextPageToken') or any(r.get('status') == 'CREATING' for r in revisions.get('revisions', [])):
        raise InterventionRequired('Неоднозначное состояние ревизий; дождись завершения операции')
    active = [r for r in revisions.get('revisions', []) if r.get('status') == 'ACTIVE']
    if len(active) > 1:
        raise InterventionRequired('Несколько активных ревизий')
    return active[0] if active else {}


def deploy_worker(cloud, manifest, *, mode='worker'):
    check_manifest(manifest)
    expected = revision_payload(manifest, mode=mode)
    current = active_revision(cloud)
    if not matches(current, expected):
        # Не повторяй неидемпотентный POST при потере ответа.
        operation = cloud.json(CONTROL + '/containers/v1/revisions:deploy', expected)
        cloud.wait(operation)
        current = active_revision(cloud)
        if not matches(current, expected):
            raise InterventionRequired('Фактическая конфигурация ревизии отличается')
    return current['id']


def health(cloud, manifest, *, mode='worker'):
    result = cloud.json(target.URL + '/health')
    if (result.get('state') != 'ready' or result.get('release_id') != manifest['release_id']
            or result.get('mode') != mode or not re.fullmatch(r'[a-f0-9]{64}', result.get('checksum', ''))):
        raise ValueError('Health другого выпуска/режима')
    return result


def run_steps(cloud, manifest):
    ready = health(cloud, manifest)
    base = {'release_id': manifest['release_id'], 'checksum': ready['checksum'],
            'job_id': 'migration_' + manifest['release_id']}
    def call(operation):
        return cloud.json(target.URL + '/v1/migrations', base | {'operation': operation})
    def validate(result):
        if result.get('state') == 'not_found' and result.get('job_id') == base['job_id']:
            return
        if (any(result.get(k) != base[k] for k in ('job_id', 'release_id', 'checksum'))
                or result.get('revision') != manifest['migration']['revision']):
            raise ValueError('Статус другого задания/выпуска/схемы')
    def after_ambiguous_response():
        observed = call('status')
        validate(observed)
        if observed.get('state') not in {'succeeded', 'needs_resume'}:
            raise InterventionRequired('Неоднозначный исход шага: остановись и проверь lease') from None
        return observed
    result = call('status')
    validate(result)
    for _ in range(8):
        if result.get('state') == 'succeeded':
            # Последнее чтение снова проверяет реальную схему внутри worker.
            confirmed = call('status')
            validate(confirmed)
            if confirmed.get('state') != 'succeeded':
                raise InterventionRequired('Успех задания больше не подтверждён')
            return {'state': 'succeeded', **base, 'revision': confirmed['revision'], 'generation': confirmed['generation']}
        if result.get('state') not in {'not_found', 'needs_resume'}:
            raise InterventionRequired('Задание running/repair/unknown: требуется разбор без автоматического recovery')
        try:
            result = call('step')
        except HTTPError as error:
            code = error.code
            error.close()
            if code not in {500, 502, 503, 504}:
                raise InterventionRequired('Worker отклонил шаг; автоматический повтор запрещён') from None
            result = after_ambiguous_response()
        except (URLError, TimeoutError, OSError):
            # После потерянного ответа читай durable status, не повторяй вслепую step.
            result = after_ambiguous_response()
        validate(result)
    raise InterventionRequired('Лимит ограниченных шагов исчерпан')


def execute(token, release_id, manifest_digest, *, bootstrap=False):
    manifest = read_manifest(token, release_id, manifest_digest)
    cloud = Cloud(token)
    if bootstrap:
        if os.environ.get('GITHUB_ACTIONS'):
            raise ValueError('Первоначальный bootstrap выполняет только оператор')
        deploy_worker(cloud, manifest, mode='bootstrap')
        ready = health(cloud, manifest, mode='bootstrap')
        result = cloud.json(target.URL + '/v1/migrations', {'operation': 'bootstrap',
                            'release_id': release_id, 'checksum': ready['checksum']})
        if (result.get('state') != 'ready' or result.get('checksum') != ready['checksum']
                or result.get('revision') != manifest['migration']['revision']):
            raise InterventionRequired('Bootstrap не подтверждён; проверь облачный worker')
    revision = deploy_worker(cloud, manifest)
    if bootstrap:
        # Оператор подготавливает только служебные таблицы и обычный режим.
        # Прикладную additive-миграцию затем инициирует staging CI.
        return {'state': 'ready', 'release_id': release_id, 'checksum': ready['checksum'],
                'revision': manifest['migration']['revision'], 'container_revision': revision,
                'manifest_sha256': manifest_digest, 'environment': 'staging'}
    result = run_steps(cloud, manifest)
    return result | {'container_revision': revision, 'manifest_sha256': manifest_digest,
                     'main_sha': manifest['main_sha'], 'environment': 'staging'}


def main():
    if os.environ.get('GITHUB_ACTIONS'):
        context = context_from_github()
        release = os.environ['GYM_RELEASE_ID']
        if release != context['release_id']:
            raise ValueError('CI не может мигрировать другой run/release')
        result = execute(staging_token(context), release, os.environ['GYM_MANIFEST_SHA256'])
        output = Path('.local/staging-migrations.json')
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write('Миграции staging подтверждены: `' + release + '`, схема `' + result['revision'] + '`.\n')
    else:
        from infra.ydb_access import Yc
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--yc', required=True)
        parser.add_argument('--release', required=True)
        parser.add_argument('--manifest-sha256', required=True)
        parser.add_argument('--bootstrap', action='store_true')
        parser.add_argument('--as-staging-ci', action='store_true')
        args = parser.parse_args()
        if args.bootstrap and args.as_staging_ci:
            raise ValueError('Bootstrap сначала выполняет оператор отдельно от проверки CI')
        result = execute(Yc(args.yc).token(target.CI if args.as_staging_ci else None), args.release,
                         args.manifest_sha256, bootstrap=args.bootstrap)
        output = Path('.local/staging-migrations-operator.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Миграции staging остановлены: ' + type(error).__name__)
        raise SystemExit(1)
