"""Операторский допуск проверенных Яндекс-личностей выбранного окружения; не HTTP/MCP."""

import argparse
import json
import os
from uuid import uuid4

import sqlalchemy as sa

from api.database import DatabaseSettings, create_engine
from api.store import Store
from infra.ydb_access import Yc, load_config


def admit(store, reference, *, state='active'):
    if state not in {'active', 'disabled'}:
        raise ValueError('Некорректное состояние допуска')
    def write(db):
        table = db.tables['login_requests']
        rows = db.connection.execute(sa.select(table.c.key, table.c.payload).where(table.c.owner == 'yandex')).all()
        matches = [(key, json.loads(payload)) for key, payload in rows if json.loads(payload).get('reference') == reference]
        if len(matches) != 1:
            raise ValueError('Не найдена единственная подтверждённая заявка; проверь reference')
        key, _ = matches[0]
        old = db.get('admissions', 'yandex', key)
        value = {'user_id': old['user_id'] if old else 'u_' + uuid4().hex, 'state': state}
        db.put('admissions', 'yandex', key, value)
        return value
    return store.run(write)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yc', required=True)
    parser.add_argument('--environment', choices=['staging', 'production'], default='staging')
    parser.add_argument('command', choices=['pending', 'allow', 'disable'])
    parser.add_argument('--reference')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS'):
        parser.error('Операторский доступ не выполняется из CI')
    if args.environment == 'production':
        from infra.production_target import ydb_config
        config = ydb_config()
    else:
        config = load_config()
    engine = create_engine(DatabaseSettings(config['endpoint'], config['database'], 'token'), token=Yc(args.yc).token())
    try:
        store = Store(engine)
        if args.command == 'pending':
            with engine.connect() as connection:
                table = store.tables['login_requests']
                values = [json.loads(value) for value in connection.execute(sa.select(table.c.payload)).scalars()]
            print(json.dumps(values, sort_keys=True))
        else:
            if not args.reference:
                parser.error('Укажи reference подтверждённого входа конкретного приглашённого человека')
            print(json.dumps(admit(store, args.reference, state='disabled' if args.command == 'disable' else 'active')))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
