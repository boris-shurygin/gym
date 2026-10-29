"""План/применение только нового табличного ACL P07 в staging."""

import argparse
import copy
import json
import os

from infra.ydb_access import Yc, load_config, connect, iam_plan, acl_plan, apply_acl, RUNTIME

TABLE = 'gym_workout_records'


def plan(yc, driver, *, apply=False):
    config = load_config()
    if TABLE not in config['runtime_tables']:
        raise ValueError('Таблица P07 отсутствует в явной allowlist')
    if iam_plan(yc, config):
        raise ValueError('Прежний доступ YDB не подтверждён; новые IAM-роли не выдаются')
    exists = any(item.name == TABLE and item.is_table()
        for item in driver.scheme_client.list_directory(config['database']).children)
    checked = copy.deepcopy(config)
    if not exists:
        checked['runtime_tables'].remove(TABLE)
    actions = acl_plan(driver, checked)
    path = config['database'] + '/' + TABLE
    subject = config['accounts']['api'] + '@as'
    if any(action['path'] != path or action['subject'] != subject or
           set(action['permissions']) - RUNTIME for action in actions):
        raise ValueError('Обнаружено изменение вне одного ACL P07; примени отдельный согласованный план')
    if not exists:
        actions = [{'path': path, 'subject': subject, 'permissions': sorted(RUNTIME)}]
    if apply:
        if not exists:
            raise ValueError('Сначала дождись миграции 0003 из доверенного main')
        for action in actions:
            apply_acl(driver, action)
    return {'environment': 'staging', 'migration_required': not exists, 'applied': apply, 'acl': actions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', default='.local/bin/yc.exe')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Операторский доступ не выполняется из CI')
    yc = Yc(args.yc)
    with connect(load_config(), yc.token()) as driver:
        print(json.dumps(plan(yc, driver, apply=args.apply)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'complete': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
