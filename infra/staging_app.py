"""Разверни готовый API/PWA после миграций, переключи staging Gateway и проверь результат."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener

from contracts.canonical import canonical_bytes, parse_request
from infra.object_store import ImmutableStore, NoRedirect
from infra.release_artifacts import MAX_BUNDLE, MAX_OBJECT, validate_payloads
from infra.release_gate import context_from_github, staging_token
from infra.staging_migrations import Cloud, active_revision, matches, read_manifest, revision_payload, health, read_ready, InterventionRequired
from infra import staging_target as target
from infra.auth_config import configure_api


FUNCTIONS = 'https://serverless-functions.api.cloud.yandex.net'
GATEWAYS = 'https://serverless-apigateway.api.cloud.yandex.net'
FUNCTION_URL = 'https://functions.yandexcloud.net/' + target.FUNCTION
TEMPLATE = json.loads((Path(__file__).parent / 'gateway-release.json').read_text(encoding='utf-8'))
BOOTSTRAP_HASH = 'd77c21995b82cdde8ae46a61f3d32ead0e6cdb379c04c06dd02f5523eb28337d'


class AppCloud(Cloud):
    def json(self, url, payload=None, *, method=None):
        if url.startswith(FUNCTIONS + '/') or url.startswith(GATEWAYS + '/'):
            request = Request(url, method=method or ('GET' if payload is None else 'POST'),
                data=None if payload is None else canonical_bytes(payload),
                headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            with build_opener(NoRedirect()).open(request, timeout=40) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024: raise ValueError('Ответ API слишком большой')
            return parse_request(raw)
        return super().json(url, payload)


def raw_request(url, *, token=None, payload=None, headers=None):
    request = Request(url, data=None if payload is None else canonical_bytes(payload), headers={
        **({'Authorization': 'Bearer ' + token} if token else {}), **(headers or {})})
    try:
        response = build_opener(NoRedirect()).open(request, timeout=40)
    except HTTPError as error:
        response = error
    with response:
        data = response.read(MAX_OBJECT + 1)
        if len(data) > MAX_OBJECT: raise ValueError('Слишком большой smoke-ответ')
        return response.code, response.headers, data


def load_payload(token, manifest):
    store = ImmutableStore(token, target.DELIVERY)
    result, size = {}, 0
    for item in manifest['artifacts']:
        with store._open('GET', 'blobs/sha256/' + item['sha256']) as response:
            data = response.read(item['bytes'] + 1)
        size += len(data)
        if size > MAX_BUNDLE or len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Готовые байты отличаются от манифеста')
        result[item['path']] = data
    validate_payloads(result)
    return result


def version_tag(release, validation=False):
    return ('v-' if validation else 'r-') + hashlib.sha256(release.encode()).hexdigest()[:32]


def web_key(release, path):
    if not path.startswith('web/'): raise ValueError('Публикуй только файлы PWA')
    return path[4:] if path.startswith('web/assets/') else 'releases/' + release + '/' + path


def render_gateway(manifest, template_bytes, *, validation=False, target=target):
    if validation:
        if os.environ.get('GITHUB_ACTIONS') or hashlib.sha256(template_bytes).hexdigest() != BOOTSTRAP_HASH:
            raise ValueError('Validation разрешён оператору только для известного bootstrap-шаблона')
    elif parse_request(template_bytes) != TEMPLATE:
        raise ValueError('Неизвестный формат gateway artifact')
    release = manifest['release_id']
    spec = copy.deepcopy(TEMPLATE)
    spec.pop('x-gym-layout')
    spec['info']['version'] = release
    spec['servers'] = [{'url': target.ORIGIN}]
    paths = spec['paths']
    integration = {'type': 'cloud_functions', 'function_id': target.FUNCTION,
                   'tag': version_tag(release, validation), 'service_account_id': target.GATEWAY_ACCOUNT,
                   'payload_format_version': '1.0'}
    for prefix in ('api', 'auth', 'oauth', '.well-known'):
        paths['/' + prefix + '/{path+}'] = {
            'parameters': [{'name': 'path', 'in': 'path', 'required': True, 'schema': {'type': 'string'}, 'style': 'simple', 'explode': False}],
            'x-yc-apigateway-any-method': {'responses': {'default': {'description': 'Gym API'}},
                                         'x-yc-apigateway-integration': copy.deepcopy(integration)}}
    paths['/mcp'] = {'x-yc-apigateway-any-method': {'responses': {'default': {'description': 'Gym MCP'}},
                                                 'x-yc-apigateway-integration': copy.deepcopy(integration)}}
    paths['/assets/{file+}'] = {
        'parameters': [{'name': 'file', 'in': 'path', 'required': True, 'schema': {'type': 'string'}, 'style': 'simple', 'explode': False}],
        'get': {'responses': {'200': {'description': 'Immutable PWA asset'}},
                'x-yc-apigateway-integration': {'type': 'object_storage', 'bucket': target.WEB_BUCKET,
                    'object': 'assets/{file}', 'service_account_id': target.GATEWAY_ACCOUNT}}}
    for item in manifest['artifacts']:
        path = item['path']
        if not path.startswith('web/') or path.startswith('web/assets/'): continue
        route = '/' + path[4:]
        paths[route] = {'get': {'responses': {'200': {'description': 'PWA file'}},
            'x-yc-apigateway-integration': {'type': 'object_storage', 'bucket': target.WEB_BUCKET,
                'object': web_key(release, path), 'service_account_id': target.GATEWAY_ACCOUNT}}}
    paths['/'] = copy.deepcopy(paths['/index.html'])
    info = {'release_id': release, 'main_sha': manifest['main_sha'],
            'purpose': 'operator-validation' if validation else target.NAME + '-release'}
    paths['/__gym/release'] = {'get': {'responses': {'200': {'description': 'Deployment identity'}},
        'x-yc-apigateway-integration': {'type': 'dummy', 'http_code': 200,
            'http_headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
            'content': {'application/json': canonical_bytes(info).decode()}}}}
    if len(canonical_bytes(spec)) > 1024 * 1024: raise ValueError('Слишком большая спецификация Gateway')
    return spec


def verify_migrations(cloud, manifest, *, target=target):
    current = active_revision(cloud, target=target)
    if not matches(current, revision_payload(manifest, target=target)):
        raise InterventionRequired('Мигратор не соответствует этому выпуску')
    ready = health(cloud, manifest, target=target)
    result = read_ready(lambda: cloud.json(target.URL + '/v1/migrations', {'operation': 'status', 'release_id': manifest['release_id'],
        'checksum': ready['checksum'], 'job_id': 'migration_' + manifest['release_id']}))
    if (result.get('state') != 'succeeded' or result.get('release_id') != manifest['release_id']
            or result.get('checksum') != ready['checksum'] or result.get('revision') != manifest['migration']['revision']
            or result.get('job_id') != 'migration_' + manifest['release_id']):
        raise InterventionRequired('Обязательная миграция не подтверждена')


def api_payload(manifest, validation=False, *, target=target):
    artifact = next(item for item in manifest['artifacts'] if item['path'] == 'api.zip')
    return configure_api({'functionId': target.FUNCTION, 'runtime': 'python312', 'entrypoint': 'index.handler',
        'description': 'Gym ' + manifest['release_id'] + ' api-sha256=' + artifact['sha256'],
        'resources': {'memory': '268435456'}, 'executionTimeout': '30s', 'concurrency': '1',
        'serviceAccountId': target.API_ACCOUNT,
        'environment': {'GYM_ENVIRONMENT': target.NAME, 'GYM_RELEASE_ID': manifest['release_id'],
            'GYM_API_ZIP_SHA256': artifact['sha256'], 'GYM_YDB_AUTH': 'metadata',
            'GYM_YDB_ENDPOINT': target.YDB_ENDPOINT, 'GYM_YDB_DATABASE': target.YDB_PATH, 'GYM_TABLE_PREFIX': 'gym_'},
        'package': {'bucketName': target.DELIVERY, 'objectName': 'blobs/sha256/' + artifact['sha256'], 'sha256': artifact['sha256']},
        'tag': [version_tag(manifest['release_id'], validation)],
        'logOptions': {'disabled': True, 'folderId': target.FOLDER},
        'metadataOptions': {'gceHttpEndpoint': 'ENABLED', 'awsV1HttpEndpoint': 'DISABLED'}}, manifest, target)


def api_matches(version, expected):
    return (version.get('status') == 'ACTIVE'
        and all(version.get(key) == expected[key] for key in ('functionId', 'runtime', 'entrypoint', 'description',
            'resources', 'executionTimeout', 'serviceAccountId', 'environment', 'logOptions', 'metadataOptions'))
        and expected['tag'][0] in version.get('tags', []) and version.get('concurrency', '1') == '1'
        and version.get('secrets', []) == expected.get('secrets', [])
        and not any(version.get(key) for key in ('connectivity', 'mounts', 'storageMounts', 'namedServiceAccounts')))


def function_version(cloud, manifest, validation=False, *, target=target):
    expected = api_payload(manifest, validation, target=target)
    resource = cloud.json(FUNCTIONS + '/functions/v1/functions/' + target.FUNCTION)
    if resource.get('folderId') != target.FOLDER or resource.get('name') != 'gym-api': raise ValueError('Чужая функция')
    bindings = cloud.json(FUNCTIONS + '/functions/v1/functions/' + target.FUNCTION + ':listAccessBindings')
    if bindings.get('nextPageToken') or any(b.get('subject', {}).get('type') == 'system' for b in bindings.get('accessBindings', [])):
        raise ValueError('Функция не должна быть публичной')
    def find():
        result = cloud.json(FUNCTIONS + '/functions/v1/versions?functionId=' + target.FUNCTION + '&pageSize=1000')
        if result.get('nextPageToken'): raise ValueError('Неполный список версий API')
        return [v for v in result.get('versions', []) if expected['tag'][0] in v.get('tags', [])]
    versions = find()
    if not versions:
        cloud.wait(cloud.json(FUNCTIONS + '/functions/v1/versions', expected))
        versions = find()
    if len(versions) != 1 or not api_matches(versions[0], expected):
        raise InterventionRequired('Тег API связан с другой/неподтверждённой версией')
    return versions[0]['id']


def private_api_smoke(token, tag, *, target=target):
    code, _, raw = raw_request('https://functions.yandexcloud.net/' + target.FUNCTION + '?tag=' + tag + '&integration=raw', token=token,
        payload={'httpMethod': 'GET', 'path': '/api/v1/health', 'headers': {}, 'body': '', 'isBase64Encoded': False},
        headers={'Content-Type': 'application/json'})
    if code != 200: raise InterventionRequired('Приватный API не ответил успешно')
    value = parse_request(raw)
    if 'statusCode' in value:
        if value['statusCode'] != 200: raise InterventionRequired('API health неуспешен')
        value = parse_request(value['body'].encode())
    if value != {'status': 'ok'}: raise InterventionRequired('Неожиданный ответ API health')


def upload_web(token, manifest, files, *, target=target):
    store = ImmutableStore(token, target.WEB_BUCKET)
    for item in manifest['artifacts']:
        if not item['path'].startswith('web/'): continue
        key = web_key(manifest['release_id'], item['path'])
        store.put(key, files[item['path']], content_type=item['content_type'], cache_control=item['cache_control'])
        with store._open('HEAD', key) as response:
            if response.headers.get('Content-Type') != item['content_type'] or response.headers.get('Cache-Control') != item['cache_control']:
                raise InterventionRequired('Метаданные готового PWA-объекта отличаются')


def switch_gateway(cloud, spec, *, target=target):
    url = GATEWAYS + '/apigateways/v1/apigateways/' + target.GATEWAY
    resource = cloud.json(url)
    if (resource.get('folderId') != target.FOLDER or resource.get('name') != 'gym-gateway'
            or 'https://' + resource.get('domain', '') != target.ORIGIN or resource.get('connectivity')):
        raise ValueError('Gateway не соответствует staging')
    current = cloud.json(url + ':spec?format=JSON')
    if parse_request(current['openapiSpec'].encode()) != spec:
        cloud.wait(cloud.json(url, {'updateMask': 'openapiSpec', 'openapiSpec': canonical_bytes(spec).decode()}, method='PATCH'))
    confirmed = cloud.json(url + ':spec?format=JSON')
    if parse_request(confirmed['openapiSpec'].encode()) != spec: raise InterventionRequired('Gateway не подтверждён')


def auth_smoke(manifest, *, target=target):
    configured = configure_api({'environment': {}}, manifest, target)
    if not configured.get('secrets'):
        return
    code, headers, _ = raw_request(target.ORIGIN + '/auth/login?return_to=%2Fapp')
    normalized = {key.lower(): value for key, value in headers.items()}
    location = urlsplit(normalized.get('location', ''))
    query = parse_qs(location.query)
    binder = normalized.get('set-cookie', '')
    if (code != 302 or normalized.get('cache-control') != 'no-store'
            or location.scheme != 'https' or location.netloc != 'oauth.yandex.ru' or location.path != '/authorize'
            or query.get('client_id') != [configured['environment']['GYM_YANDEX_CLIENT_ID']]
            or query.get('redirect_uri') != [target.ORIGIN + '/auth/callback/yandex']
            or query.get('response_type') != ['code'] or query.get('scope') != ['login:info']
            or query.get('code_challenge_method') != ['S256']
            or len(query.get('state', [''])[0]) < 43 or len(query.get('code_challenge', [''])[0]) != 43
            or not binder.startswith('__Host-gym_login=')
            or not all(flag in binder for flag in ('Path=/', 'Secure', 'HttpOnly', 'SameSite=Lax', 'Max-Age=300'))
            or 'domain=' in binder.lower()):
        raise InterventionRequired('Начало Яндекс ID/PKCE/cookie через Gateway не подтверждено')
    # Валидная по форме, но случайная cookie заставляет runtime прочитать YDB.
    # Liveness и заведомо некорректная cookie такой проверки не обеспечивают.
    import secrets
    code, headers, body = raw_request(target.ORIGIN + '/auth/session',
        headers={'Cookie': '__Host-gym_session=' + secrets.token_urlsafe(32)})
    if (code != 401 or parse_request(body).get('error', {}).get('code') != 'session_required'
            or headers.get('Cache-Control') != 'no-store'):
        raise InterventionRequired('Проверка сессии в YDB через Gateway не подтверждена')
    code, headers, body = raw_request(target.ORIGIN + '/auth/callback/yandex?state=' + 'x' * 43 + '&code=readiness-probe')
    if (code != 400 or parse_request(body).get('error', {}).get('code') != 'invalid_request'
            or headers.get('Cache-Control') != 'no-store'):
        raise InterventionRequired('Callback без браузерной привязки должен отклоняться')


def smoke(manifest, files, *, validation=False, target=target):
    for item in manifest['artifacts']:
        if not item['path'].startswith('web/'): continue
        code, headers, body = raw_request(target.ORIGIN + '/' + item['path'][4:])
        if (code != 200 or body != files[item['path']] or headers.get('Content-Type') != item['content_type']
                or headers.get('Cache-Control') != item['cache_control']):
            raise InterventionRequired('HTTPS PWA-файл/заголовки отличаются')
    code, _, body = raw_request(target.ORIGIN + '/')
    if code != 200 or body != files['web/index.html']: raise InterventionRequired('Главная страница не соответствует выпуску')
    code, headers, body = raw_request(target.ORIGIN + '/__gym/release')
    expected = {'release_id': manifest['release_id'], 'main_sha': manifest['main_sha'],
                'purpose': 'operator-validation' if validation else target.NAME + '-release'}
    if code != 200 or parse_request(body) != expected or headers.get('Cache-Control') != 'no-store':
        raise InterventionRequired('Не совпала идентичность Gateway')
    code, headers, body = raw_request(target.ORIGIN + '/api/v1/health')
    if code != 200 or parse_request(body) != {'status': 'ok'} or headers.get('Cache-Control') != 'no-store':
        raise InterventionRequired('Health через Gateway не подтверждён')
    for path in ('/api/v1/program', '/auth/session'):
        for headers in ({}, {'Cookie': 'gym_session=forged', 'Authorization': 'Bearer forged'}):
            code, response_headers, body = raw_request(target.ORIGIN + path, headers=headers)
            if code != 401 or parse_request(body).get('error', {}).get('code') != 'session_required' or response_headers.get('Cache-Control') != 'no-store':
                raise InterventionRequired('Закрытый личный маршрут не подтверждён')
    for path in ('/api/v1/unknown-path', '/assets/missing-asset.js', '/unknown-page'):
        code, _, _ = raw_request(target.ORIGIN + path)
        if code != 404: raise InterventionRequired('Неизвестный маршрут не вернул 404')
    auth_smoke(manifest, target=target)
    for url in ('https://functions.yandexcloud.net/' + target.FUNCTION, 'https://storage.yandexcloud.net/' + target.WEB_BUCKET + '/' + web_key(manifest['release_id'], 'web/index.html')):
        code, _, _ = raw_request(url)
        if code != 403: raise InterventionRequired('Backend не является приватным')


def execute(token, release, manifest_digest, *, validation=False, target=target, schema_manifest=None):
    if validation and (os.environ.get('GITHUB_ACTIONS') or target.NAME != 'staging'):
        raise ValueError('Validation Gateway разрешён оператору только для staging')
    manifest = read_manifest(token, release, manifest_digest)
    files = load_payload(token, manifest)
    spec = render_gateway(manifest, files['gateway.json'], validation=validation, target=target)
    cloud = AppCloud(token, target=target)
    if schema_manifest is not None:
        # Откат только к совместимому приложению; worker/схема остаются текущими.
        if (schema_manifest['migration']['revision'] != manifest['migration']['revision']
                or schema_manifest['compatibility'] != manifest['compatibility']):
            raise InterventionRequired('Откат приложения несовместим с текущей схемой')
    verify_migrations(cloud, schema_manifest if schema_manifest is not None else manifest, target=target)
    version = function_version(cloud, manifest, validation, target=target)
    private_api_smoke(token, version_tag(release, validation), target=target)
    upload_web(token, manifest, files, target=target)
    switch_gateway(cloud, spec, target=target)
    # Повторяй только чтение smoke во время распространения нового Gateway.
    for attempt in range(12):
        try:
            smoke(manifest, files, validation=validation, target=target)
            break
        except InterventionRequired:
            if attempt == 11: raise
            time.sleep(5)
    receipt = {'format': 1, 'environment': target.NAME, 'release_id': release, 'main_sha': manifest['main_sha'],
        'manifest_sha256': manifest_digest, 'function_version': version, 'function_tag': version_tag(release, validation),
        'gateway_sha256': hashlib.sha256(canonical_bytes(spec)).hexdigest(),
        'purpose': 'operator-validation' if validation else target.NAME + '-release', 'smoke': 'succeeded', 'origin': target.ORIGIN}
    key = 'deployments/' + release + ('/operator-validation.json' if validation else '/result.json')
    ImmutableStore(token, target.WEB_BUCKET).put(key, canonical_bytes(receipt), content_type='application/json', cache_control='private, no-store')
    return receipt


def main():
    if os.environ.get('GITHUB_ACTIONS'):
        context = context_from_github()
        release = os.environ['GYM_RELEASE_ID']
        if release != context['release_id']: raise ValueError('Другой выпуск/попытка CI')
        result = execute(staging_token(context), release, os.environ['GYM_MANIFEST_SHA256'])
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write('Staging: ' + target.ORIGIN + '\n\nВыпуск `' + release + '`, smoke успешен.\n')
    else:
        from infra.ydb_access import Yc
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--yc', required=True)
        parser.add_argument('--release', required=True)
        parser.add_argument('--manifest-sha256', required=True)
        parser.add_argument('--validation-gateway', action='store_true')
        args = parser.parse_args()
        result = execute(Yc(args.yc).token(target.CI), args.release, args.manifest_sha256, validation=args.validation_gateway)
    output = Path('.local/staging-deployment.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        detail = ': ' + str(error) if isinstance(error, InterventionRequired) else ''
        if isinstance(error, HTTPError): detail = ': HTTP ' + str(error.code)
        print('Выкатка staging остановлена: ' + type(error).__name__ + detail)
        raise SystemExit(1)
