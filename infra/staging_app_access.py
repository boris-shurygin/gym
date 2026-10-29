"""Спланируй/примени точечные права API/PWA/Gateway только существующего staging."""

import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request, build_opener

from infra import staging_access, staging_target as target
from infra.delivery_access import OPERATOR
from infra.migration_container import cloud
from infra.object_store import NoRedirect
from infra.ydb_access import Yc
from infra.storage_operations import wait as wait_bucket


BINDINGS = [
    (('serverless', 'function'), target.FUNCTION, 'functions.editor', target.CI),
    (('serverless', 'function'), target.FUNCTION, 'functions.functionInvoker', target.CI),
    (('serverless', 'function'), target.FUNCTION, 'functions.functionInvoker', target.GATEWAY_ACCOUNT),
    (('serverless', 'api-gateway'), target.GATEWAY, 'api-gateway.editor', target.CI),
    (('iam', 'service-account'), target.API_ACCOUNT, 'iam.serviceAccounts.user', target.CI),
    (('iam', 'service-account'), target.GATEWAY_ACCOUNT, 'iam.serviceAccounts.user', target.CI),
]


def web_policy(*, target=target, production_reader=False):
    bucket = 'arn:aws:s3:::' + target.WEB_BUCKET
    actor = {'CanonicalUser': target.CI}
    public = [bucket + '/assets/*', bucket + '/releases/*/web/*']
    writable = public + [bucket + '/deployments/*']
    result = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'OperatorControl', 'Effect': 'Allow', 'Principal': {'CanonicalUser': OPERATOR},
         'Action': 's3:*', 'Resource': [bucket, bucket + '/*']},
        {'Sid': 'GatewayReadPublicFiles', 'Effect': 'Allow', 'Principal': {'CanonicalUser': target.GATEWAY_ACCOUNT},
         'Action': 's3:GetObject', 'Resource': public},
        {'Sid': 'CiRead', 'Effect': 'Allow', 'Principal': actor, 'Action': 's3:GetObject', 'Resource': writable},
        {'Sid': 'CiCreate', 'Effect': 'Allow', 'Principal': actor, 'Action': 's3:PutObject', 'Resource': writable,
         'Condition': {'StringEquals': {'s3:if-none-match': '*'}}},
        {'Sid': 'RequireCondition', 'Effect': 'Deny', 'Principal': actor, 'Action': 's3:PutObject', 'Resource': bucket + '/*',
         'Condition': {'Null': {'s3:if-none-match': 'true'}}},
        {'Sid': 'RequireStar', 'Effect': 'Deny', 'Principal': actor, 'Action': 's3:PutObject', 'Resource': bucket + '/*',
         'Condition': {'StringNotEquals': {'s3:if-none-match': '*'}}},
        {'Sid': 'NoDeleteOrAcl', 'Effect': 'Deny', 'Principal': actor,
         'Action': ['s3:DeleteObject', 's3:DeleteBucket', 's3:PutBucketPolicy', 's3:DeleteBucketPolicy', 's3:PutObjectAcl', 's3:PutBucketAcl'],
         'Resource': [bucket, bucket + '/*']},
    ]}
    if production_reader:
        if target.NAME != 'staging': raise ValueError('Квитанции staging читаются только из staging')
        result['Statement'].append({'Sid': 'ProductionReadStagingReceipts', 'Effect': 'Allow',
            'Principal': {'CanonicalUser': 'aje5mg9tp1cachquiqlf'}, 'Action': 's3:GetObject',
            'Resource': bucket + '/deployments/*/result.json'})
    return result


def bucket_api(yc, action, body=None):
    if action not in {'listAccessBindings', 'updateAccessBindings'}:
        raise ValueError('Неизвестный IAM-метод')
    request = Request('https://storage.api.cloud.yandex.net/storage/v1/buckets/' + target.WEB_BUCKET_ID + ':' + action,
        method='GET' if body is None else 'PATCH', data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + yc.token(), 'Content-Type': 'application/json'})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        return json.load(response)


def plan(yc):
    if yc.run('iam', 'whoami') != OPERATOR:
        raise ValueError('Используй согласованного оператора')
    previous = staging_access.plan(yc)
    if any(previous[k] for k in ('set_delivery_policy', 'add_container_editor', 'add_migrator_user', 'add_delivery_reader')):
        raise ValueError('Сначала восстанови права мигратора отдельно')
    bucket = yc.run('storage', 'bucket', 'get', target.WEB_BUCKET)
    if (bucket.get('resource_id') != target.WEB_BUCKET_ID or bucket.get('folder_id') != target.FOLDER
            or bucket.get('anonymous_access_flags') != {'read': False, 'list': False, 'config_read': False}
            or bucket.get('acl', {}).get('grants')):
        raise ValueError('Бакет не соответствует закрытому staging')
    policy = bucket.get('policy')
    if isinstance(policy, str): policy = json.loads(policy)
    if policy is not None and policy not in (web_policy(), web_policy(production_reader=True)):
        raise ValueError('Не заменяй неизвестную web-policy')
    missing = []
    for group, identifier, role, actor in BINDINGS:
        resource = (yc.run(*group, 'get', identifier) if group[0] == 'iam' else cloud(yc, *group, 'get', '--id', identifier))
        if resource.get('folder_id') != target.FOLDER or resource.get('name') not in {'gym-api', 'gym-gateway', 'gym-staging-api', 'gym-staging-gateway'}:
            raise ValueError('Ресурс не соответствует staging')
        bindings = (yc.run(*group, 'list-access-bindings', identifier) if group[0] == 'iam'
                    else cloud(yc, *group, 'list-access-bindings', '--id', identifier))
        allowed = {r for g, i, r, a in BINDINGS if g == group and i == identifier and a == actor}
        own = {b['role_id'] for b in bindings if b.get('subject', {}).get('id') == actor}
        if own - allowed or any(b.get('subject', {}).get('type') == 'system' for b in bindings):
            raise ValueError('Обнаружены лишние/публичные права')
        if role not in own: missing.append({'group': list(group), 'resource': identifier, 'role': role, 'account': actor})
    result = bucket_api(yc, 'listAccessBindings')
    if result.get('nextPageToken'): raise ValueError('Неполный список IAM бакета')
    bucket_missing = []
    for actor, role in [(target.CI, 'storage.uploader'), (target.GATEWAY_ACCOUNT, 'storage.viewer')]:
        own = {b['roleId'] for b in result.get('accessBindings', []) if b.get('subject', {}).get('id') == actor}
        if own - {role}: raise ValueError('Лишние роли web-бакета')
        if role not in own: bucket_missing.append({'roleId': role, 'subject': {'type': 'serviceAccount', 'id': actor}})
    if any(b.get('subject', {}).get('type') == 'system' for b in result.get('accessBindings', [])):
        raise ValueError('Публичный IAM бакета запрещён')
    return {'set_web_policy': policy is None, 'bindings': missing, 'bucket_bindings': bucket_missing,
            'scope': 'staging-only', 'limits': 'editor включает удаление целевых API/Gateway; actAs API/Gateway SA; публикация синтетического PWA по HTTPS'}


def apply(yc):
    changes = plan(yc)
    if changes['set_web_policy']:
        with tempfile.TemporaryDirectory(prefix='gym-web-policy-') as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(web_policy()), encoding='utf-8')
            yc.run('storage', 'bucket', 'update', target.WEB_BUCKET, '--policy-from-file', str(path))
    actual = yc.run('storage', 'bucket', 'get', target.WEB_BUCKET).get('policy')
    if isinstance(actual, str): actual = json.loads(actual)
    if actual not in (web_policy(), web_policy(production_reader=True)): raise ValueError('Ограничивающая политика не подтверждена')
    for binding in changes['bucket_bindings']:
        operation = bucket_api(yc, 'updateAccessBindings', {'accessBindingDeltas': [{'action': 'ADD', 'accessBinding': binding}]})
        wait_bucket(yc, operation)
    for binding in changes['bindings']:
        args = [*binding['group'], 'add-access-binding']
        args += [binding['resource']] if args[0] == 'iam' else ['--id', binding['resource']]
        args += ['--role', binding['role'], '--service-account-id', binding['account']]
        yc.run(*args) if args[0] == 'iam' else cloud(yc, *args)
    confirmed = plan(yc)
    if confirmed['set_web_policy'] or confirmed['bindings'] or confirmed['bucket_bindings']:
        raise RuntimeError('Изменения прав не подтверждены')
    return confirmed


def main():
    if os.environ.get('GITHUB_ACTIONS'): raise ValueError('IAM применяет только локальный оператор')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    yc = Yc(args.yc)
    print(json.dumps(apply(yc) if args.apply else plan(yc), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
