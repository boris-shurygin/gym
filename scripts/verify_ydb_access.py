"""Проверь реальные права staging на собственных временных таблицах, затем удали их."""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ydb
from infra.ydb_access import CONFIG, ROOT, RUNTIME, Yc, connect, load_config, reconcile


def access_denied_issue(issues):
    for issue in issues or ():
        if issue.issue_code == 2028 and 'status: accessdenied' in issue.message.lower():
            return True
        if access_denied_issue(issue.issues):
            return True
    return False


def denied(operation):
    """Сетевой сбой или синтаксическая ошибка не доказывают запрет доступа."""
    try:
        operation()
    except ydb.Unauthorized:
        return 'UNAUTHORIZED'
    except ydb.Aborted as error:
        # YDB Serverless иногда оборачивает запрет разрешения таблицы в Aborted.
        # Требуй структурированный resolve issue с явным AccessDenied.
        if access_denied_issue(error.issues):
            return 'ABORTED: resolve AccessDenied'
        raise
    except ydb.ConnectionLost as error:
        # SDK 3.33.2 оборачивает gRPC PERMISSION_DENIED в ConnectionLost.
        # Не принимай за отказ любой ConnectionLost, timeout или UNAVAILABLE.
        if 'status = StatusCode.PERMISSION_DENIED' in str(error):
            return 'gRPC PERMISSION_DENIED'
        raise
    except ydb.SchemeError as error:
        if 'access denied' in str(error).lower():
            return 'SCHEME_ERROR: access denied'
        raise
    raise AssertionError('Запрещённая операция неожиданно разрешена')


def execute(driver, text, *, scheme=False):
    """Ограничь каждую сессию/RPC; пул не должен бесконечно повторять отказ."""
    settings = ydb.BaseRequestSettings().with_timeout(15).with_operation_timeout(12)
    session = driver.table_client.session().create(settings)
    try:
        if scheme:
            return session.execute_scheme(text, settings)
        return session.transaction(ydb.SerializableReadWrite()).execute(text, commit_tx=True, settings=settings)
    finally:
        try:
            session.delete(settings)
        except ydb.Error:
            pass  # Удаление сессии после разрыва связи — best effort.


def execute_as(config, token, text, *, scheme=False):
    # SDK после gRPC PERMISSION_DENIED отключает канал. Для следующей
    # независимой отрицательной проверки создай новый драйвер, не повтор RPC.
    with connect(config, token, disable_discovery=True) as driver:
        return execute(driver, text, scheme=scheme)


def verify(yc, config):
    checks = []
    with connect(config, yc.token()) as operator:
        plan = reconcile(yc, config, operator)
        if plan['iam'] or plan['acl']:
            raise ValueError('Сначала примени и повторно проверь план прав')
        prefix = 'gym_acl_' + uuid.uuid4().hex + '_'
        app, admin, forbidden = [config['database'] + '/' + prefix + s
                                 for s in ('app', 'admin', 'forbidden')]
        candidates = (app, admin, forbidden)
        initial = {entry.name for entry in operator.scheme_client.list_directory(config['database']).children}
        if any(path.rsplit('/', 1)[1] in initial for path in candidates):
            raise AssertionError('Временные имена уже заняты')
        try:
            with connect(config, yc.token(config['accounts']['migrations'])) as migration, \
                    connect(config, yc.token(config['accounts']['api'])) as api:

                def ddl(driver, query):
                    return execute(driver, query, scheme=True)

                def query(driver, text):
                    return execute(driver, text)

                for path in (app, admin):
                    ddl(migration, f'CREATE TABLE `{path}` (id Utf8 NOT NULL, value Int64, PRIMARY KEY (id))')
                    time.sleep(1.5)  # Уважай ограничение частоты DDL Serverless.
                checks.append({'check': 'migration_create', 'result': 'allowed'})
                ddl(migration, f'ALTER TABLE `{app}` ADD COLUMN note Utf8')
                api_subject = config['accounts']['api'] + '@as'
                migration.scheme_client.modify_permissions(app,
                    ydb.ModifyPermissionsSettings().grant_permissions(api_subject, sorted(RUNTIME))
                    .with_timeout(15).with_operation_timeout(12))
                checks.append({'check': 'migration_alter_and_grant', 'result': 'allowed'})
                query(api, f'UPSERT INTO `{app}` (id, value) VALUES ("probe", 1)')
                query(api, f'UPDATE `{app}` SET value = 2 WHERE id = "probe"')
                rows = query(api, f'SELECT value FROM `{app}` WHERE id = "probe"')[0].rows
                if len(rows) != 1 or rows[0].value != 2:
                    raise AssertionError('Запись API не подтверждена чтением')
                query(api, f'DELETE FROM `{app}` WHERE id = "probe"')
                if query(api, f'SELECT value FROM `{app}`')[0].rows:
                    raise AssertionError('Удаление API не подтверждено')
                checks.append({'check': 'api_insert_update_select_delete', 'result': 'allowed'})
                for name, operation in [
                    ('api_create', lambda: ddl(api, f'CREATE TABLE `{forbidden}` (id Utf8 NOT NULL, PRIMARY KEY (id))')),
                    ('api_alter', lambda: ddl(api, f'ALTER TABLE `{app}` ADD COLUMN forbidden Int64')),
                    ('api_drop', lambda: ddl(api, f'DROP TABLE `{app}`')),
                    ('api_grant', lambda: api.scheme_client.modify_permissions(app,
                        ydb.ModifyPermissionsSettings().grant_permissions(api_subject, ['ydb.granular.alter_schema'])
                        .with_timeout(15).with_operation_timeout(12))),
                    ('api_admin_read', lambda: query(api, f'SELECT * FROM `{admin}`')),
                    ('api_admin_write', lambda: query(api, f'UPSERT INTO `{admin}` (id, value) VALUES ("probe", 1)')),
                ]:
                    checks.append({'check': name, 'result': denied(operation)})
                # CI не получает ни DML, ни DDL к этой базе. Ошибка аутентификации
                # не принимается за успешную проверку изоляции.
                ci_accounts = ('aje0omi931fri66pbdir', 'aje1ia9cf3t1g7o5p6pg', 'aje5mg9tp1cachquiqlf')
                for index, account in enumerate(ci_accounts):
                    # Прямой endpoint даёт точный YDB UNAUTHORIZED вместо
                    # обёртки ConnectionFailure на отказе discovery.
                    token = yc.token(account)
                    for suffix, operation in [
                        ('read', lambda: execute_as(config, token, f'SELECT * FROM `{app}`')),
                        ('write', lambda: execute_as(config, token, f'UPSERT INTO `{app}` (id, value) VALUES ("ci", 1)')),
                        ('ddl', lambda: execute_as(config, token, f'ALTER TABLE `{app}` ADD COLUMN ci_forbidden Int64', scheme=True)),
                    ]:
                        checks.append({'check': f'ci_{index}_{suffix}', 'result': denied(operation)})
                time.sleep(1.5)
                ddl(migration, f'DROP TABLE `{admin}`')
                checks.append({'check': 'migration_drop', 'result': 'allowed'})
        finally:
            # Очищай только три проверенных случайных имени этого запуска,
            # в том числе после неожиданно успешного запрещённого CREATE.
            for path in candidates:
                entries = {e.name for e in operator.scheme_client.list_directory(config['database']).children}
                if path.rsplit('/', 1)[1] in entries:
                    time.sleep(1.5)
                    execute(operator, f'DROP TABLE `{path}`', scheme=True)
            remaining = {e.name for e in operator.scheme_client.list_directory(config['database']).children}
            if any(p.rsplit('/', 1)[1] in remaining for p in candidates):
                raise AssertionError('Временные таблицы не удалены')
        # После очистки у API не остаётся выдававшихся временной таблице ACL.
        after = reconcile(yc, config, operator)
        if after['iam'] or after['acl']:
            raise AssertionError('После проверки найдено расхождение прав')
    return {'environment': 'staging', 'checks': checks, 'temporary_tables_removed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', default=str(ROOT / '.local/bin/yc.exe'))
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS'):
        raise ValueError('Этот стенд запускает только оператор вне GitHub Actions')
    result = verify(Yc(args.yc), load_config(CONFIG))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Проверка прав остановлена: ' + type(error).__name__)
        raise SystemExit(1)
