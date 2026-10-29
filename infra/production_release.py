"""Продвинь проверенные пакеты или откати совместимое приложение через защищённый workflow."""

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError

from contracts.canonical import canonical_bytes, parse_request
from infra import production_gate as gate, production_target as production, staging_app as app
from infra import staging_migrations as migrations
from infra.object_store import ImmutableStore


BOOTSTRAP = parse_request((Path(__file__).parent / 'gateway-bootstrap.json').read_bytes())


def progress(phase, target, release):
    if os.environ.get('GITHUB_ACTIONS'):
        print(json.dumps({'event':'production_release','phase':phase,'environment':target.NAME,'release_id':release}),flush=True)


def read_object(store, key):
    try:
        with store._open('GET', key) as response:
            raw = response.read(65537)
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 404:
            return None
        raise
    if len(raw) > 65536:
        raise ValueError('Слишком большой служебный объект')
    value = parse_request(raw)
    if raw != canonical_bytes(value):
        raise ValueError('Неканоническая служебная запись')
    return value


def installed(cloud, target):
    url = app.GATEWAYS + '/apigateways/v1/apigateways/' + target.GATEWAY
    resource = cloud.json(url)
    if (resource.get('folderId') != target.FOLDER or resource.get('name') != 'gym-gateway'
            or 'https://' + resource.get('domain', '') != target.ORIGIN or resource.get('connectivity')):
        raise ValueError('Gateway не соответствует окружению')
    spec = parse_request(cloud.json(url + ':spec?format=JSON')['openapiSpec'].encode())
    digest = hashlib.sha256(canonical_bytes(spec)).hexdigest()
    if spec in (BOOTSTRAP, BOOTSTRAP | {'servers': [{'url': target.ORIGIN}]}):
        return None, digest
    try:
        info = parse_request(spec['paths']['/__gym/release']['get']['x-yc-apigateway-integration']['content']['application/json'].encode())
        gate.identity(info['release_id'])
        if (info['main_sha'] != info['release_id'][:40] or info['purpose'] != target.NAME + '-release'):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('Неизвестное состояние установленного приложения') from None
    return info['release_id'], digest


def worker_state(token, cloud, target, *, verified=None):
    active = migrations.active_revision(cloud, target=target)
    if not active:
        return None, None
    release = active.get('image', {}).get('environment', {}).get('GYM_RELEASE_ID', '')
    manifest, _ = verified(release) if verified is not None else gate.staged_release(token, release)
    if not migrations.matches(active, migrations.revision_payload(manifest, target=target)):
        raise migrations.InterventionRequired('Активный worker имеет неожиданную конфигурацию')
    ready = migrations.health(cloud, manifest, target=target)
    status = migrations.read_ready(lambda: cloud.json(target.URL + '/v1/migrations', {
        'operation': 'status', 'release_id': release, 'checksum': ready['checksum'], 'job_id': 'migration_' + release}))
    if status.get('job_id') != 'migration_' + release:
        raise ValueError('Статус чужого migration job')
    if status.get('state') not in {'not_found', 'succeeded', 'needs_resume'}:
        raise migrations.InterventionRequired('Worker running/repair/unknown: сначала восстанови точное задание')
    if status['state'] != 'not_found' and (status.get('release_id') != release
            or status.get('checksum') != ready['checksum'] or status.get('revision') != manifest['migration']['revision']):
        raise ValueError('Не совпала идентичность migration status')
    return manifest, status['state']


def execute(token, release, operation, operation_id, *, target=production):
    if operation not in {'promote', 'rollback'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', operation_id):
        raise ValueError('Некорректная операция/идентификатор')
    checked = {}
    def verified(identifier):
        if identifier not in checked:
            checked[identifier] = gate.staged_release(token, identifier)
        return checked[identifier]
    manifest, staging_receipt = verified(release)
    progress('candidate-verified',target,release)
    if manifest['migration']['revision'] != '0001_runtime_metadata':
        raise ValueError('Этот процесс пока поддерживает только проверенную короткую ревизию схемы')
    cloud = app.AppCloud(token, target=target)
    previous, previous_digest = installed(cloud, target)
    store = ImmutableStore(token, target.WEB_BUCKET)
    if previous:
        previous_manifest, _ = verified(previous)
        previous_spec = app.render_gateway(previous_manifest, canonical_bytes(app.TEMPLATE), target=target)
        if hashlib.sha256(canonical_bytes(previous_spec)).hexdigest() != previous_digest:
            raise ValueError('Установленный Gateway отличается от своего проверенного выпуска')
    schema_manifest, state = worker_state(token, cloud, target, verified=verified)
    if schema_manifest and (schema_manifest['migration']['revision'] != manifest['migration']['revision']
            or schema_manifest['compatibility'] != manifest['compatibility']):
        raise migrations.InterventionRequired('Сначала согласуй переход от фактической схемы')
    if schema_manifest and schema_manifest['release_id'] != release and state != 'succeeded':
        raise migrations.InterventionRequired('Сначала заверши прежний migration job тем же выпуском')
    if operation == 'rollback':
        receipt = read_object(store, 'deployments/' + release + '/result.json')
        if (not previous or not schema_manifest or state != 'succeeded' or not receipt
                or receipt.get('environment') != target.NAME or receipt.get('release_id') != release
                or receipt.get('manifest_sha256') != staging_receipt['manifest_sha256']
                or receipt.get('purpose') != target.NAME + '-release' or receipt.get('smoke') != 'succeeded'):
            raise ValueError('Откат допускается только к ранее установленному совместимому выпуску')
    key = 'deployments/operations/' + operation_id
    intent = read_object(store, key + '/intent.json')
    if intent is None:
        intent = {'format': 1, 'environment': target.NAME, 'operation': operation, 'release_id': release,
            'manifest_sha256': staging_receipt['manifest_sha256'], 'previous_release_id': previous,
            'previous_gateway_sha256': previous_digest, 'staging_run_id': manifest['build']['run_id']}
        store.put(key + '/intent.json', canonical_bytes(intent), content_type='application/json', cache_control='private, no-store')
    elif (intent.get('environment') != target.NAME or intent.get('operation') != operation
            or intent.get('release_id') != release or intent.get('manifest_sha256') != staging_receipt['manifest_sha256']
            or previous not in {intent.get('previous_release_id'), release}):
        raise ValueError('Повтор относится к другой операции/состоянию окружения')
    if os.environ.get('GITHUB_ACTIONS'):
        Path('.local').mkdir(exist_ok=True)
        Path('.local/production-plan.json').write_text(json.dumps(intent, indent=2) + '\n', encoding='utf-8')
    if operation == 'promote':
        progress('migrations',target,release)
        migrations.execute(token, release, staging_receipt['manifest_sha256'], target=target)
        schema_manifest = manifest
    progress('application',target,release)
    result = app.execute(token, release, staging_receipt['manifest_sha256'], target=target,
        schema_manifest=schema_manifest if operation == 'rollback' else None)
    audit = {'format': 1, 'environment': target.NAME, 'operation_id': operation_id, 'operation': operation,
        'release_id': release, 'previous_release_id': intent['previous_release_id'],
        'schema_release_id': schema_manifest['release_id'], 'deployment': result}
    store.put(key + '/result.json', canonical_bytes(audit), content_type='application/json', cache_control='private, no-store')
    progress('succeeded',target,release)
    return audit


def main():
    if not os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Выпуск production запускается только настроенным GitHub workflow')
    context = gate.context_from_dispatch()
    result = execute(gate.production_token(context), context['release_id'], context['operation'],
        str(context['run_id']) + '_' + str(context['attempt']))
    Path('.local/production-release.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write('Production: ' + production.ORIGIN + '\n\n' + context['operation'] + ': `' + context['release_id'] + '`, smoke успешен.\n')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        detail = ': ' + str(error) if isinstance(error, migrations.InterventionRequired) else ''
        if isinstance(error, HTTPError): detail = ': HTTP ' + str(error.code)
        print('Выпуск production остановлен: ' + type(error).__name__ + detail)
        raise SystemExit(1)
