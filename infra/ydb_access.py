"""Планируй/применяй точечные права staging; не вызывай из PR или HTTP API."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import ydb


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'infra/ydb-access.json'
RUNTIME = frozenset({
    'ydb.granular.describe_schema', 'ydb.granular.select_row',
    'ydb.granular.update_row', 'ydb.granular.erase_row',
})
READ_ONLY = frozenset({'ydb.granular.describe_schema', 'ydb.granular.select_row'})
MIGRATIONS = RUNTIME | {
    'ydb.granular.create_table', 'ydb.granular.alter_schema',
    'ydb.granular.remove_schema', 'ydb.access.grant',
}
ADMIN_TABLES = {'gym_migration_lock', 'gym_migration_jobs', 'gym_migration_requests', 'gym_alembic_version', 'gym_schema_version'}


def load_config(path=CONFIG):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    expected = {'schema_version', 'environment', 'cloud_id', 'folder_id',
                'database_id', 'endpoint', 'database', 'accounts', 'runtime_tables'}
    if (set(config) - {'runtime_readonly_tables'} != expected
            or type(config['schema_version']) is not int or config['schema_version'] != 1):
        raise ValueError('Неизвестный формат конфигурации прав')
    # Этот инструмент намеренно не принимает production или произвольную БД.
    if (config['environment'] != 'staging'
            or config['cloud_id'] != 'b1ghmudqihbvpnscd2fq'
            or config['folder_id'] != 'b1ghdeobqrevsim6jg9o'
            or config['database_id'] != 'etn63tldguvkj5kcpi3r'
            or config['endpoint'] != 'grpcs://ydb.serverless.yandexcloud.net:2135'
            or config['database'] != '/ru-central1/b1ghmudqihbvpnscd2fq/etn63tldguvkj5kcpi3r'
            or config['accounts'] != {'api': 'ajeffa6opd0qko82m4de',
                                      'migrations': 'ajedn74q9psjfecqnteg'}):
        raise ValueError('Конфигурация не соответствует согласованному staging')
    tables = config['runtime_tables']
    if (not isinstance(tables, list) or any(not isinstance(t, str) or not
            re.fullmatch(r'gym_[a-z][a-z0-9_]{0,90}', t) or t in ADMIN_TABLES for t in tables)
            or len(set(tables)) != len(tables)):
        raise ValueError('Явно перечисли только прикладные таблицы без административных')
    readonly = config.get('runtime_readonly_tables', [])
    if (not isinstance(readonly, list) or readonly not in ([], ['gym_admissions'])
            or 'gym_admissions' in tables or set(tables) & set(readonly)):
        raise ValueError('Таблица допуска доступна runtime только на чтение')
    return config


class Yc:
    def __init__(self, executable):
        self.executable = str(Path(executable).resolve())

    def run(self, *args):
        result = subprocess.run([self.executable, *args, '--format', 'json'],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            # CLI stderr может содержать диагностические детали авторизации.
            raise RuntimeError('Ошибка YC ' + ' '.join(args[:3]) + '; проверь авторизацию/доступ локально')
        return json.loads(result.stdout) if result.stdout.strip() else None

    def token(self, account=None):
        args = [self.executable, 'iam', 'create-token']
        if account:
            args += ['--impersonate-service-account-id', account]
        result = subprocess.run(args, capture_output=True, text=True, timeout=90)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError('Не удалось получить временный IAM-токен')
        return result.stdout.strip()


def connect(config, token, *, disable_discovery=False):
    driver = ydb.Driver(ydb.DriverConfig(
        endpoint=config['endpoint'], database=config['database'],
        credentials=ydb.AccessTokenCredentials(token), disable_discovery=disable_discovery))
    try:
        driver.wait(timeout=20, fail_fast=True)
    except Exception:
        driver.stop()
        raise
    return driver


def own_permissions(entry, subject):
    return {name for p in entry.permissions if p.subject == subject for name in p.permission_names}


def acl_delta(entry, subject, desired):
    current = own_permissions(entry, subject)
    if current - desired:
        raise ValueError('Обнаружены лишние прямые права; исправь расхождение явно')
    if entry.owner == subject:
        raise ValueError('Целевой аккаунт владеет объектом; минимальные права не доказаны')
    return sorted(desired - current)


def acl_plan(driver, config):
    """Не удаляй чужие ACL; отклони лишние права целевых аккаунтов до изменений."""
    root = config['database']
    api = config['accounts']['api'] + '@as'
    migration = config['accounts']['migrations'] + '@as'
    entry = driver.scheme_client.describe_path(root)
    acl_delta(entry, api, set())
    actions = []
    missing = acl_delta(entry, migration, MIGRATIONS)
    if missing:
        actions.append({'path': root, 'subject': migration, 'permissions': missing})
    # Проверяй все существующие пользовательские объекты, включая права,
    # которые иначе могли бы сохраниться после удаления таблицы из allowlist.
    def walk(path):
        for child in driver.scheme_client.list_directory(path).children:
            if child.name == '.sys':
                continue  # Виртуальные системные пути, не объекты приложения.
            full = path + '/' + child.name
            item = driver.scheme_client.describe_path(full)
            if item.interrupt_permission_inheritance:
                raise ValueError('Наследование ACL отключено; права мигратора не доказаны')
            desired = (RUNTIME if path == root and child.name in config['runtime_tables'] else
                READ_ONLY if path == root and child.name in config.get('runtime_readonly_tables', []) else set())
            # Allowlist поддерживает только таблицы непосредственно в корне.
            if desired and not item.is_table():
                raise ValueError('Прикладной объект не является таблицей')
            missing = acl_delta(item, api, desired)
            if own_permissions(item, migration):
                raise ValueError('Лишние прямые ACL мигратора на дочернем объекте')
            if missing:
                actions.append({'path': full, 'subject': api, 'permissions': missing})
            if child.is_directory():
                walk(full)
    walk(root)
    existing = {c.name for c in driver.scheme_client.list_directory(root).children if c.is_table()}
    if (set(config['runtime_tables']) | set(config.get('runtime_readonly_tables', []))) - existing:
        raise ValueError('Прикладная таблица ещё не создана; сначала выполни миграцию')
    return actions


def iam_plan(yc, config):
    database = yc.run('ydb', 'database', 'get', '--id', config['database_id'])
    if (database['folder_id'] != config['folder_id'] or database['name'] != 'gym'
            or database.get('labels', {}).get('gym-project') != 'gym'):
        raise ValueError('База не соответствует ресурсу Gym staging')
    folder = yc.run('resource-manager', 'folder', 'get', '--id', config['folder_id'])
    if folder['cloud_id'] != config['cloud_id']:
        raise ValueError('Каталог находится в другом облаке')
    cloud = yc.run('resource-manager', 'cloud', 'get', '--id', config['cloud_id'])
    desired = set(config['accounts'].values())
    scopes = [('resource-manager', 'folder', config['folder_id']),
              ('resource-manager', 'cloud', config['cloud_id']),
              ('organization-manager', 'organization', cloud['organization_id'])]
    for service, resource, identifier in scopes:
        for binding in yc.run(service, resource, 'list-access-bindings', '--id', identifier):
            subject = binding['subject']
            if (subject.get('id') in desired or subject.get('type') == 'system') and binding['role_id'] not in {'auditor', 'ydb.auditor'}:
                raise ValueError('Наследуемые IAM-права шире минимальных; проверь их явно')
    for key, account in config['accounts'].items():
        actual = yc.run('iam', 'service-account', 'get', '--id', account)
        if actual['folder_id'] != config['folder_id'] or actual['name'] != 'gym-staging-' + key:
            raise ValueError('Сервисный аккаунт не соответствует staging')
    bindings = yc.run('ydb', 'database', 'list-access-bindings', '--id', config['database_id'])
    for binding in bindings:
        subject = binding['subject']
        if (subject.get('id') in desired or subject.get('type') == 'system') and binding['role_id'] != 'ydb.auditor':
            raise ValueError('Лишняя IAM-роль на базе; не расширяй права автоматически')
    return [{'account': account, 'role': 'ydb.auditor'} for account in sorted(desired)
            if not any(b['role_id'] == 'ydb.auditor' and b['subject'].get('id') == account
                       for b in bindings)]


def apply_acl(driver, action):
    settings = ydb.ModifyPermissionsSettings().grant_permissions(action['subject'], action['permissions'])
    driver.scheme_client.modify_permissions(action['path'], settings)


def reconcile(yc, config, driver, *, apply=False):
    iam = iam_plan(yc, config)
    acl = acl_plan(driver, config)
    # Все доступные проверки выполняются до первой мутации.
    if apply:
        for action in iam:
            yc.run('ydb', 'database', 'add-access-binding', '--id', config['database_id'],
                   '--role', action['role'], '--service-account-id', action['account'])
        for action in acl:
            apply_acl(driver, action)
    return {'environment': 'staging', 'applied': apply, 'iam': iam, 'acl': acl}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--yc', default=str(ROOT / '.local/bin/yc.exe'))
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Управление ACL запускает оператор вне GitHub Actions')
    config = load_config()
    yc = Yc(args.yc)
    with connect(config, yc.token()) as driver:
        print(json.dumps(reconcile(yc, config, driver, apply=args.apply), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Не выводи исключения SDK/CLI, способные содержать credentials.
        print('Настройка доступа остановлена: ' + type(error).__name__)
        raise SystemExit(1)
