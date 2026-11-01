"""Проверь штатные копии и изолированное восстановление только staging, вне CI."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import ydb

from infra.ydb_access import Yc, connect, load_config

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / '.local/p11-restore.json'
TABLES = {'gym_admissions', 'gym_alembic_version', 'gym_login_requests', 'gym_login_transactions',
    'gym_migration_jobs', 'gym_migration_lock', 'gym_migration_requests', 'gym_program_records',
    'gym_runtime_metadata', 'gym_schema_version', 'gym_sessions', 'gym_workout_records'}


def target_path(config, name):
    if not re.fullmatch(r'gym_p11_restore_[0-9]{8}_[0-9]{6}', name):
        raise ValueError('Разрешена только новая изолированная область проверки P11')
    return config['database'] + '/' + name


def backup_status(yc, config):
    database = yc.run('ydb', 'database', 'get', config['database_id'])
    backups = yc.run('ydb', 'backup', 'list', '--folder-id', config['folder_id'])
    ready = [b for b in backups if b.get('database_id') == config['database_id'] and b.get('status') == 'READY'
        and b.get('description') != 'SYSTEM_SHADOW']
    latest = max(ready, key=lambda b: b['created_at']) if ready else None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(latest['created_at'].replace('Z', '+00:00'))).total_seconds() if latest else None
    settings = database.get('backup_config', {}).get('backup_settings', [])
    return {'database_id': config['database_id'], 'backup_id': latest['id'] if latest else None,
        'snapshot_at': latest['created_at'] if latest else None, 'snapshot_age_seconds': round(age) if age is not None else None,
        'completed_at': latest.get('completed_at') if latest else None,
        'freshness_basis': 'backup_created_at_conservative',
        'running_backup_ids': [b['id'] for b in backups if b.get('database_id') == config['database_id'] and b.get('status') == 'CREATING'],
        'daily_7_days': any('daily_backup_schedule' in s.get('backup_schedule', {})
            and s.get('backup_time_to_live') == '604800s' for s in settings),
        'fresh': age is not None and age <= 86400, 'deletion_protection': database.get('deletion_protection', False)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'start', 'verify', 'cleanup'])
    parser.add_argument('--yc', default='.local/bin/yc.exe')
    parser.add_argument('--target')
    args = parser.parse_args()
    config, yc = load_config(), Yc(args.yc)
    if args.action == 'status':
        print(json.dumps(backup_status(yc, config))); return
    if args.action == 'start':
        status = backup_status(yc, config)
        if not status['backup_id'] or not status['daily_7_days']:
            raise ValueError('Нет готовой штатной копии/расписания')
        if status['running_backup_ids']:
            raise ValueError('В исходной базе идут backup-задания; restore запрещён YC до их завершения')
        path = target_path(config, args.target or '')
        if RECEIPT.exists() and not json.loads(RECEIPT.read_text(encoding='utf-8')).get('cleaned'):
            raise ValueError('Предыдущая проверка ещё не завершена; сначала проверь её состояние')
        with connect(config, yc.token(config['accounts']['migrations'])) as driver:
            if args.target in {c.name for c in driver.scheme_client.list_directory(config['database']).children}:
                raise ValueError('Целевой путь уже существует; перезапись запрещена')
        # Намерение сохраняется до запроса: при неопределённом исходе не запускай второй restore.
        record = {'target': args.target, 'database_id': config['database_id'], 'backup_id': status['backup_id'],
            'snapshot_at': status['snapshot_at'], 'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'starting'}
        RECEIPT.parent.mkdir(exist_ok=True)
        RECEIPT.write_text(json.dumps(record), encoding='utf-8')
        operation = yc.run('ydb', 'database', 'restore', config['database_id'], '--backup-id', status['backup_id'],
            '--target-path', path, '--async')
        record.update(operation_id=operation['id'], state='restoring')
        RECEIPT.write_text(json.dumps(record), encoding='utf-8')
        print(json.dumps(record)); return
    record = json.loads(RECEIPT.read_text(encoding='utf-8'))
    if record['database_id'] != config['database_id'] or args.target != record['target'] or record.get('cleaned'):
        raise ValueError('Цель не совпадает с незавершённой операторской проверкой')
    path = target_path(config, record['target'])
    operation = yc.run('operation', 'get', record['operation_id'])
    if not operation.get('done'):
        print(json.dumps({'state': 'restoring', 'operation_id': record['operation_id']})); return
    if operation.get('error'):
        raise ValueError('Restore завершился ошибкой: ' + str(operation['error'].get('code')))
    with connect(config, yc.token(config['accounts']['migrations'])) as driver:
        children = driver.scheme_client.list_directory(path).children
        names = {c.name for c in children}
        if names != TABLES or any(not c.is_table() for c in children):
            raise ValueError('Восстановленная схема отличается; очистка автоматически запрещена')
        if args.action == 'verify':
            counts = {}
            with ydb.SessionPool(driver, size=1) as pool:
                for name in sorted(TABLES):
                    def read(session):
                        # Только счётчик; значения пользователей остаются в YDB.
                        return session.transaction().execute(f'SELECT COUNT(*) AS n FROM `{path}/{name}`;', commit_tx=True)[0].rows[0].n
                    counts[name] = pool.retry_operation_sync(read)
            # Проверяй отрицательный доступ API к восстановленным сессиям/истории.
            with connect(config, yc.token(config['accounts']['api'])) as api_driver:
                with ydb.SessionPool(api_driver, size=1) as pool:
                    try:
                        pool.retry_operation_sync(lambda session: session.transaction().execute(
                            f'SELECT COUNT(*) FROM `{path}/gym_sessions`;', commit_tx=True))
                    except (ydb.Unauthorized, ydb.SchemeError):
                        pass
                    else:
                        raise ValueError('Runtime не должен читать восстановленные таблицы')
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(record['started_at'])).total_seconds()
            record.update(state='verified', tables=counts, isolation_verified=True, elapsed_seconds=round(elapsed), rto_met=elapsed <= 14400)
        else:
            if record.get('state') != 'verified' or not record.get('isolation_verified'):
                raise ValueError('Удаление допускается только после проверки')
            # Каждый абсолютный путь проверен выше и принадлежит только этой изолированной копии.
            with ydb.SessionPool(driver, size=1) as pool:
                for name in sorted(TABLES):
                    pool.retry_operation_sync(lambda session, name=name: session.drop_table(path + '/' + name))
            driver.scheme_client.remove_directory(path)
            record.update(cleaned=True)
    RECEIPT.write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
