"""Минимальная служебная схема, без пользовательских таблиц."""

import re
import sqlalchemy as sa


def tables(prefix='gym_'):
    if not re.fullmatch(r'[a-z][a-z0-9_]{2,63}_', prefix):
        raise ValueError('Некорректный префикс таблиц')
    metadata = sa.MetaData()
    lock = sa.Table(prefix + 'migration_lock', metadata,
                    sa.Column('name', sa.Text, primary_key=True, nullable=False),
                    sa.Column('owner', sa.Text), sa.Column('acquired_at', sa.BigInteger))
    jobs = sa.Table(prefix + 'migration_jobs', metadata,
                    sa.Column('job_id', sa.Text, primary_key=True, nullable=False),
                    sa.Column('target', sa.Text), sa.Column('checksum', sa.Text),
                    sa.Column('state', sa.Text), sa.Column('updated_at', sa.BigInteger))
    runtime = sa.Table(prefix + 'runtime_metadata', metadata,
                       sa.Column('key', sa.Text, primary_key=True, nullable=False),
                       sa.Column('value', sa.Text))
    return lock, jobs, runtime


class SchemaMismatch(RuntimeError):
    pass


def verify_table(connection, expected):
    # Reflection dialect сводит Int32/Int64 к одному SQLAlchemy Integer.
    # Для проверки ширины используй точные типы describe из того же DBAPI.
    description = connection.connection.driver_connection.describe(expected.name)
    actual = {column.name: str(column.type) for column in description.columns}
    if set(actual) != set(expected.columns.keys()):
        raise SchemaMismatch('Набор столбцов отличается от ожидаемого')
    for column in expected.columns:
        found = actual[column.name]
        expected_type = connection.dialect.type_compiler.process(column.type).upper()
        expected_type = {'BIGINT': 'INT64'}.get(expected_type, expected_type)
        if found.rstrip('?').upper() != expected_type:
            raise SchemaMismatch('Тип столбца отличается от ожидаемого')
        if found.endswith('?') != column.nullable:
            raise SchemaMismatch('Nullable столбца отличается от ожидаемого')
    if list(description.primary_key) != [c.name for c in expected.primary_key]:
        raise SchemaMismatch('Первичный ключ отличается от ожидаемого')


def ensure_table(engine, table):
    # DDL не должен выполняться внутри DML-транзакции.
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        try:
            table.create(connection, checkfirst=True)
        except sa.exc.DBAPIError:
            # Параллельный первый запуск мог создать таблицу между describe/create.
            # При любой другой ошибке describe/проверка ниже также завершатся ошибкой.
            if not sa.inspect(connection).has_table(table.name):
                raise
        verify_table(connection, table)
