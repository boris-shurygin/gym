"""План/применение точных прав аккаунтов staging, без чтения секретов."""

import argparse
import json
import os
from pathlib import Path

from infra import staging_target as target
from infra.auth_config import configure_api
from infra.ydb_access import Yc, load_config, connect, reconcile, apply_acl


def secret_plan(yc, *, target=target):
    configured = configure_api({'environment': {}}, {'migration': {'revision': '0002_accounts_programs'}}, target)
    if not configured.get('secrets'):
        raise ValueError('Сначала укажи точные метаданные OAuth staging')
    reference = configured['secrets'][0]
    resource = yc.run('lockbox', 'secret', 'get', '--id', reference['id'])
    version = resource.get('current_version', {})
    if (resource.get('folder_id') != target.FOLDER or resource.get('name') != 'gym-' + target.NAME + '-yandex-id'
            or resource.get('status') != 'ACTIVE'
            or resource.get('labels', {}) != {'gym-project': 'gym', 'gym-environment': target.NAME,
                'gym-client-id': configured['environment']['GYM_YANDEX_CLIENT_ID']}
            or version.get('id') != reference['versionId'] or version.get('status') != 'ACTIVE'
            or version.get('payload_entry_keys') != ['client_secret']):
        raise ValueError('Секрет/версия/клиент не соответствуют staging')
    account = yc.run('iam', 'service-account', 'get', '--id', target.API_ACCOUNT)
    if account.get('folder_id') != target.FOLDER or account.get('name') != 'gym-' + target.NAME + '-api':
        raise ValueError('Чужой сервисный аккаунт')
    bindings = yc.run('lockbox', 'secret', 'list-access-bindings', '--id', reference['id']) or []
    for binding in bindings:
        subject = binding['subject']
        if (subject.get('type') == 'system' or subject.get('id') == target.CI
                or subject.get('id') == target.API_ACCOUNT and binding['role_id'] != 'lockbox.payloadViewer'):
            raise ValueError('У секрета обнаружены лишние права API/CI/анонимного доступа')
    granted = any(binding['role_id'] == 'lockbox.payloadViewer'
        and binding['subject'] == {'id': target.API_ACCOUNT, 'type': 'serviceAccount'} for binding in bindings)
    return [] if granted else [{'secret_id': reference['id'], 'account': target.API_ACCOUNT,
                                'role': 'lockbox.payloadViewer'}]


def plan(yc, driver, *, apply=False):
    # Все проверки, включая существование таблиц после миграции, до мутаций.
    lockbox = secret_plan(yc)
    config = load_config()
    database = reconcile(yc, config, driver, apply=False)
    if apply:
        for action in database['iam']:
            yc.run('ydb', 'database', 'add-access-binding', '--id', config['database_id'],
                '--role', action['role'], '--service-account-id', action['account'])
        for action in database['acl']:
            apply_acl(driver, action)
        for action in lockbox:
            yc.run('lockbox', 'secret', 'add-access-binding', '--id', action['secret_id'],
                '--role', action['role'], '--service-account-id', action['account'])
    return {'environment': 'staging', 'applied': apply, 'lockbox': lockbox,
            'database': {**database, 'applied': apply}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', default=str(Path(__file__).resolve().parents[1] / '.local/bin/yc.exe'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Точный доступ применяет оператор вне CI')
    yc = Yc(args.yc)
    config = load_config()
    with connect(config, yc.token()) as driver:
        print(json.dumps(plan(yc, driver, apply=args.apply), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Настройка доступа остановлена: ' + type(error).__name__)
        raise SystemExit(1)
