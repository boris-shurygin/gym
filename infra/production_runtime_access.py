"""Проверь/выдай только табличные права приложения и доступ к OAuth production."""
import argparse
import copy
import json
import os

from infra import production_target as target
from infra.accounts_access import secret_plan
from infra.ydb_access import Yc, connect, acl_plan, apply_acl, RUNTIME, READ_ONLY

WRITABLE = ('gym_login_transactions', 'gym_sessions', 'gym_login_requests',
            'gym_program_records', 'gym_workout_records')
READABLE = ('gym_admissions',)


def runtime_config():
    return target.ydb_config() | {'runtime_tables': list(WRITABLE), 'runtime_readonly_tables': list(READABLE)}


def table_plan(driver):
    config = runtime_config()
    existing = {item.name for item in driver.scheme_client.list_directory(config['database']).children if item.is_table()}
    checked = copy.deepcopy(config)
    checked['runtime_tables'] = sorted(set(WRITABLE) & existing)
    checked['runtime_readonly_tables'] = sorted(set(READABLE) & existing)
    actions = acl_plan(driver, checked)
    allowed = {config['database'] + '/' + table: RUNTIME for table in WRITABLE}
    allowed.update({config['database'] + '/' + table: READ_ONLY for table in READABLE})
    subject = target.API_ACCOUNT + '@as'
    if any(action['subject'] != subject or action['path'] not in allowed
           or not set(action['permissions']) <= allowed[action['path']] for action in actions):
        raise ValueError('Изменение вне точного табличного плана production')
    for actor in (target.CI, target.GATEWAY_ACCOUNT):
        isolated = config | {'accounts': config['accounts'] | {'api': actor},
                            'runtime_tables': [], 'runtime_readonly_tables': []}
        if acl_plan(driver, isolated):
            raise ValueError('Прежние права мигратора/изоляция CI и Gateway не подтверждены')
    missing = sorted((set(WRITABLE) | set(READABLE)) - existing)
    for table in missing:
        actions.append({'path': config['database'] + '/' + table, 'subject': subject,
                        'permissions': sorted(allowed[config['database'] + '/' + table])})
    return {'acl': actions, 'migration_required': bool(missing)}


def plan(yc, driver, *, apply=False):
    database = yc.run('ydb', 'database', 'get', '--id', target.DATABASE)
    if database.get('folder_id') != target.FOLDER or database.get('name') != 'gym':
        raise ValueError('Чужая production-база')
    lockbox = secret_plan(yc, target=target)
    tables = table_plan(driver)
    if apply:
        if tables['migration_required']:
            raise ValueError('Сначала заверши prepare через production workflow; таблицы не созданы')
        for action in tables['acl']:
            apply_acl(driver, action)
        for action in lockbox:
            yc.run('lockbox', 'secret', 'add-access-binding', '--id', action['secret_id'],
                   '--role', action['role'], '--service-account-id', action['account'])
        confirmed = plan(yc, driver)
        if confirmed['acl'] or confirmed['lockbox']:
            raise ValueError('Права ещё не подтверждены; перечитай состояние перед повтором')
    return {'environment': 'production', 'applied': apply, **tables, 'lockbox': lockbox}


def main():
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Точные права применяет оператор вне CI')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', default='.local/bin/yc.exe')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    yc = Yc(args.yc)
    with connect(target.ydb_config(), yc.token()) as driver:
        print(json.dumps(plan(yc, driver, apply=args.apply)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'complete': False, 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
