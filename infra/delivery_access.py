"""Спланируй точечные права издателя delivery; применяй только локальным оператором."""

import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request, build_opener

from infra.object_store import NoRedirect
from infra.ydb_access import Yc


BUCKET = 'gym-delivery-b1ghmudqihbvpnscd2fq'
BUCKET_ID = 'e3e9b3p987mraociqtio'
FOLDER = 'b1gtp00mbao11pppvkk8'
REGISTRY = 'crpe91o8ka9e03n3n90c'
REPOSITORY = 'crpbvi15g2kc6667ksdh'
PUBLISHER = 'aje0omi931fri66pbdir'
OPERATOR = 'aje01oganeq2se1li11l'
PUSHER = 'container-registry.images.pusher'
UPLOADER = 'storage.uploader'


def bucket_api(yc, action, body=None):
    if action not in {'listAccessBindings', 'updateAccessBindings'}:
        raise ValueError('Неизвестный метод IAM бакета')
    request = Request('https://storage.api.cloud.yandex.net/storage/v1/buckets/' + BUCKET_ID + ':' + action,
        method='GET' if body is None else 'PATCH', data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + yc.token(), 'Content-Type': 'application/json'})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        return json.load(response)


def policy(*, staging_reader=True):
    bucket = 'arn:aws:s3:::' + BUCKET
    principal = {'CanonicalUser': PUBLISHER}
    result = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'OperatorControl', 'Effect': 'Allow', 'Principal': {'CanonicalUser': OPERATOR},
         'Action': 's3:*', 'Resource': [bucket, bucket + '/*']},
        {'Sid': 'PublisherRead', 'Effect': 'Allow', 'Principal': principal,
         'Action': 's3:GetObject', 'Resource': [bucket + '/blobs/sha256/*', bucket + '/releases/*']},
        {'Sid': 'PublisherCreateOnly', 'Effect': 'Allow', 'Principal': principal,
         'Action': 's3:PutObject', 'Resource': [bucket + '/blobs/sha256/*', bucket + '/releases/*'],
         'Condition': {'StringEquals': {'s3:if-none-match': '*'}}},
        {'Sid': 'PublisherRequireCondition', 'Effect': 'Deny', 'Principal': principal,
         'Action': 's3:PutObject', 'Resource': bucket + '/*',
         'Condition': {'Null': {'s3:if-none-match': 'true'}}},
        {'Sid': 'PublisherRequireStar', 'Effect': 'Deny', 'Principal': principal,
         'Action': 's3:PutObject', 'Resource': bucket + '/*',
         'Condition': {'StringNotEquals': {'s3:if-none-match': '*'}}},
        {'Sid': 'PublisherNoDeleteOrAdmin', 'Effect': 'Deny', 'Principal': principal,
         'Action': ['s3:DeleteObject', 's3:DeleteBucket', 's3:PutBucketPolicy', 's3:DeleteBucketPolicy',
                    's3:PutObjectAcl', 's3:PutBucketAcl'], 'Resource': [bucket, bucket + '/*']},
    ]}
    if staging_reader:
        result['Statement'].append({'Sid': 'StagingReleaseReader', 'Effect': 'Allow',
            'Principal': {'CanonicalUser': 'aje1ia9cf3t1g7o5p6pg'}, 'Action': 's3:GetObject',
            'Resource': [bucket + '/blobs/sha256/*', bucket + '/releases/*']})
    return result


def desired_binding():
    return {'role_id': PUSHER, 'subject': {'id': PUBLISHER, 'type': 'serviceAccount'}}


def check_bindings(bindings, allowed):
    own = {b['role_id'] for b in bindings if b.get('subject', {}).get('id') == PUBLISHER}
    if own - set(allowed) or any(b.get('subject', {}).get('type') == 'system' for b in bindings):
        raise ValueError('Обнаружены неожиданные/публичные права; автоматическое изменение остановлено')
    return own


def plan(yc):
    if yc.run('iam', 'whoami') != OPERATOR:
        raise ValueError('Проверь учётную запись первоначального оператора')
    bucket = yc.run('storage', 'bucket', 'get', BUCKET)
    if (bucket.get('folder_id') != FOLDER or bucket.get('resource_id') != BUCKET_ID
            or bucket.get('acl', {}).get('grants') or bucket.get('anonymous_access_flags') !=
            {'read': False, 'list': False, 'config_read': False}):
        raise ValueError('Бакет не соответствует закрытому delivery')
    existing = bucket.get('policy')
    if isinstance(existing, str):
        existing = json.loads(existing)
    if existing is not None and existing not in (policy(), policy(staging_reader=False)):
        raise ValueError('Не заменяй неизвестную политику бакета')
    repository = yc.run('container', 'repository', 'get', REPOSITORY)
    if repository != {'id': REPOSITORY, 'name': REGISTRY + '/gym-migrations'}:
        raise ValueError('Неожиданный репозиторий образов')
    for command, resource, allowed in [
        (('resource-manager', 'cloud'), 'b1ghmudqihbvpnscd2fq', set()),
        (('organization-manager', 'organization'), 'bpf0t8ve1h1ckdn59bot', set()),
        (('resource-manager', 'folder'), FOLDER, {'auditor'}),
        (('container', 'registry'), REGISTRY, set()),
        (('resource-manager', 'folder'), 'b1ghdeobqrevsim6jg9o', set()),
        (('resource-manager', 'folder'), 'b1gt4o1311mhe57522j8', set()),
    ]:
        check_bindings(yc.run(*command, 'list-access-bindings', resource), allowed)
    roles = check_bindings(yc.run('container', 'repository', 'list-access-bindings', REPOSITORY), {PUSHER})
    bucket_bindings = bucket_api(yc, 'listAccessBindings')
    if bucket_bindings.get('nextPageToken'):
        raise ValueError('Неожиданное число привязок IAM бакета')
    bucket_roles = check_bindings([{'role_id': b['roleId'], 'subject': b['subject']}
                                  for b in bucket_bindings.get('accessBindings', [])], {UPLOADER})
    return {'bucket': BUCKET, 'publisher': PUBLISHER, 'policy': policy(),
            'set_bucket_policy': existing != policy(), 'repository': REPOSITORY,
            'add_bucket_binding': UPLOADER not in bucket_roles,
            'add_repository_binding': PUSHER not in roles, 'binding': desired_binding(),
            'provider_limit': 'pusher также разрешает удаление образов/репозитория; delete не выполняется'}


def apply(yc):
    changes = plan(yc)
    if changes['set_bucket_policy']:
        with tempfile.TemporaryDirectory(prefix='gym-delivery-policy-') as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(changes['policy']), encoding='utf-8')
            yc.run('storage', 'bucket', 'update', BUCKET, '--policy-from-file', str(path))
    # Не выдавай базовый IAM до подтверждения ограничивающей политики.
    protected = yc.run('storage', 'bucket', 'get', BUCKET)
    actual_policy = protected.get('policy')
    if isinstance(actual_policy, str):
        actual_policy = json.loads(actual_policy)
    if actual_policy != policy() or protected.get('acl', {}).get('grants'):
        raise ValueError('Политика/отсутствие обходных ACL не подтверждены')
    if changes['add_bucket_binding']:
        operation = bucket_api(yc, 'updateAccessBindings', {'accessBindingDeltas': [
            {'action': 'ADD', 'accessBinding': {'roleId': UPLOADER,
                'subject': {'id': PUBLISHER, 'type': 'serviceAccount'}}}]})
        if not operation.get('done'):
            operation = yc.run('operation', 'get', operation['id'], '--endpoint', 'storage.api.cloud.yandex.net:443')
        if not operation.get('done') or operation.get('error'):
            raise RuntimeError('Операция IAM бакета не подтверждена; проверь фактические привязки')
    if changes['add_repository_binding']:
        yc.run('container', 'repository', 'add-access-binding', REPOSITORY,
               '--role', PUSHER, '--service-account-id', PUBLISHER)
    result = plan(yc)
    if result['set_bucket_policy'] or result['add_repository_binding'] or result['add_bucket_binding']:
        raise RuntimeError('Применение прав не подтверждено')
    return result


def main():
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Права выдаёт только локальный оператор, не CI')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    yc = Yc(args.yc)
    print(json.dumps(apply(yc) if args.apply else plan(yc), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
