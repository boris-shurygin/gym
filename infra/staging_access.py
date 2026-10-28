"""План/выдача прав CI только на приватный мигратор staging и чтение выпуска."""

import argparse
import json
import os
from pathlib import Path
import tempfile

from infra import delivery_access as delivery
from infra import staging_target as target
from infra.migration_container import cloud
from infra.ydb_access import Yc


EDITOR = 'serverless-containers.editor'
ACT_AS = 'iam.serviceAccounts.user'
READER = 'storage.viewer'


def own_roles(bindings, allowed):
    own = {b['role_id'] for b in bindings if b.get('subject', {}).get('id') == target.CI}
    if own - set(allowed) or any(b.get('subject', {}).get('type') == 'system' for b in bindings):
        raise ValueError('Неожиданные/публичные права CI; разбери расхождение до применения')
    return own


def plan(yc):
    protected = delivery.plan(yc)
    if protected['add_bucket_binding'] or protected['add_repository_binding']:
        raise ValueError('Сначала восстанови согласованные права издателя отдельно')
    container = cloud(yc, 'serverless', 'container', 'get', '--id', target.CONTAINER)
    account = yc.run('iam', 'service-account', 'get', target.MIGRATOR)
    if (container.get('folder_id') != target.FOLDER or container.get('name') != 'gym-migrations'
            or container.get('labels', {}).get('gym-project') != 'gym'
            or account.get('folder_id') != target.FOLDER or account.get('name') != 'gym-staging-migrations'):
        raise ValueError('Мигратор/SA не соответствуют staging')
    container_roles = own_roles(cloud(yc, 'serverless', 'container', 'list-access-bindings', '--id', target.CONTAINER), {EDITOR})
    account_roles = own_roles(yc.run('iam', 'service-account', 'list-access-bindings', target.MIGRATOR), {ACT_AS})
    bucket = delivery.bucket_api(yc, 'listAccessBindings')
    if bucket.get('nextPageToken'):
        raise ValueError('Неполный список привязок бакета')
    bucket_roles = own_roles([{'role_id': b['roleId'], 'subject': b['subject']}
                             for b in bucket.get('accessBindings', [])], {READER})
    for command, resource, allowed in [
        (('resource-manager', 'cloud'), 'b1ghmudqihbvpnscd2fq', set()),
        (('organization-manager', 'organization'), 'bpf0t8ve1h1ckdn59bot', set()),
        (('resource-manager', 'folder'), target.FOLDER, {'auditor'}),
        (('resource-manager', 'folder'), delivery.FOLDER, set()),
        (('resource-manager', 'folder'), 'b1gt4o1311mhe57522j8', set()),
    ]:
        own_roles(yc.run(*command, 'list-access-bindings', resource), allowed)
    return {'environment': 'staging', 'account': target.CI, 'container': target.CONTAINER,
            'migrator': target.MIGRATOR, 'delivery_bucket': target.DELIVERY,
            'set_delivery_policy': protected['set_bucket_policy'],
            'add_container_editor': EDITOR not in container_roles,
            'add_migrator_user': ACT_AS not in account_roles,
            'add_delivery_reader': READER not in bucket_roles,
            'limits': 'editor включает удаление контейнера; actAs разрешает запуск кода с DDL-правами мигратора; CI не получает прямых прав DDL/DML в YDB'}


def apply(yc):
    changes = plan(yc)
    if changes['set_delivery_policy']:
        with tempfile.TemporaryDirectory(prefix='gym-staging-reader-') as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(delivery.policy()), encoding='utf-8')
            yc.run('storage', 'bucket', 'update', target.DELIVERY, '--policy-from-file', str(path))
    policy = yc.run('storage', 'bucket', 'get', target.DELIVERY).get('policy')
    if isinstance(policy, str):
        policy = json.loads(policy)
    if policy != delivery.policy():
        raise ValueError('Ограничивающая политика не подтверждена')
    if changes['add_delivery_reader']:
        operation = delivery.bucket_api(yc, 'updateAccessBindings', {'accessBindingDeltas': [
            {'action': 'ADD', 'accessBinding': {'roleId': READER, 'subject': {'id': target.CI, 'type': 'serviceAccount'}}}]})
        if not operation.get('done'):
            operation = yc.run('operation', 'get', operation['id'], '--endpoint', 'storage.api.cloud.yandex.net:443')
        if not operation.get('done') or operation.get('error'):
            raise RuntimeError('Проверь фактическую привязку IAM бакета перед повтором')
    if changes['add_migrator_user']:
        yc.run('iam', 'service-account', 'add-access-binding', target.MIGRATOR,
               '--role', ACT_AS, '--service-account-id', target.CI)
    if changes['add_container_editor']:
        cloud(yc, 'serverless', 'container', 'add-access-binding', '--id', target.CONTAINER,
              '--role', EDITOR, '--service-account-id', target.CI)
    confirmed = plan(yc)
    if any(confirmed[key] for key in ('set_delivery_policy', 'add_delivery_reader', 'add_migrator_user', 'add_container_editor')):
        raise RuntimeError('Применение прав не подтверждено')
    return confirmed


def main():
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Права изменяет только локальный оператор')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    yc = Yc(args.yc)
    print(json.dumps(apply(yc) if args.apply else plan(yc), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
