"""Транзакционное хранилище небольших JSON-документов, без DDL в runtime."""

import json
import re

import sqlalchemy as sa

from api.database import transaction
from contracts.canonical import canonical_bytes

NAMES = ('admissions', 'login_transactions', 'sessions', 'login_requests', 'program_records')


def application_tables(prefix='gym_'):
    if not re.fullmatch(r'[a-z][a-z0-9_]{2,63}_', prefix):
        raise ValueError('Некорректный префикс')
    return {name: sa.Table(prefix + name, sa.MetaData(),
        sa.Column('owner', sa.Text, primary_key=True, nullable=False),
        sa.Column('key', sa.Text, primary_key=True, nullable=False),
        sa.Column('payload', sa.Text, nullable=False)) for name in NAMES}


class Documents:
    def __init__(self, connection, tables):
        self.connection, self.tables = connection, tables

    def get(self, name, owner, key):
        table = self.tables[name]
        value = self.connection.execute(sa.select(table.c.payload).where(
            table.c.owner == owner, table.c.key == key)).scalar_one_or_none()
        return json.loads(value) if value is not None else None

    def put(self, name, owner, key, value):
        table = self.tables[name]
        payload = canonical_bytes(value).decode('utf-8')
        # SELECT + INSERT/UPDATE внутри одной SERIALIZABLE транзакции; не UPSERT
        # с потерей проверки предусловий. Повторяется только подтверждённый abort.
        if self.get(name, owner, key) is None:
            self.connection.execute(table.insert().values(owner=owner, key=key, payload=payload))
        else:
            self.connection.execute(table.update().where(table.c.owner == owner,
                table.c.key == key).values(payload=payload))

    def delete(self, name, owner, key):
        table = self.tables[name]
        self.connection.execute(table.delete().where(table.c.owner == owner, table.c.key == key))


class Store:
    def __init__(self, engine, prefix='gym_'):
        self.engine, self.tables = engine, application_tables(prefix)

    def run(self, operation):
        return transaction(self.engine, lambda connection: operation(Documents(connection, self.tables)))
