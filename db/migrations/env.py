"""Конфигурация Alembic для проверенных DDL без фиктивной общей транзакции."""

from alembic import context
from alembic.ddl.impl import DefaultImpl
import sqlalchemy as sa
from api.database import transaction
from db.schema import ensure_table, verify_table, schema_version_table, tables, SchemaMismatch


class YDBImpl(DefaultImpl):
    __dialect__ = 'yql'
    transactional_ddl = False

    def version_table_impl(self, *, version_table, version_table_schema, version_table_pk, **kw):
        # Публичный hook Alembic. YDB запрещает UPDATE PK, поэтому номер
        # ревизии — обычный столбец, а PK постоянен: name='schema'.
        if not version_table.endswith('schema_version'):
            raise ValueError('Неизвестная таблица версий')
        return schema_version_table(version_table.removesuffix('schema_version'))


attributes = context.config.attributes
attributes['guard']()
version = schema_version_table(attributes['prefix'])
legacy = sa.Table(attributes['prefix'] + 'alembic_version', sa.MetaData(),
    sa.Column('version_num', sa.Text, primary_key=True, nullable=False))
with attributes['engine'].connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
    previous = []
    if sa.inspect(connection).has_table(legacy.name):
        verify_table(connection, legacy)
        previous = connection.execute(sa.select(legacy.c.version_num)).scalars().all()
        if previous not in ([], ['0001_runtime_metadata']):
            raise SchemaMismatch('Неизвестная legacy-ревизия; автоматический переход запрещён')
        if previous:
            verify_table(connection, tables(attributes['prefix'])[2])
ensure_table(attributes['engine'], version)


def bridge(connection):
    rows = connection.execute(sa.select(version.c.name, version.c.version_num)).all()
    if not rows and previous:
        connection.execute(version.insert().values(name='schema', version_num=previous[0]))
    elif rows and (len(rows) != 1 or rows[0][0] != 'schema'):
        raise SchemaMismatch('Таблица версии должна содержать одну строку schema')


# Только под migration lock. Старую таблицу не удаляй/не переписывай: её
# последняя проверенная 0001 остаётся свидетельством предыдущего формата.
transaction(attributes['engine'], bridge)
attributes['guard']()
with attributes['engine'].connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
    context.configure(connection=connection,
                      version_table=version.name,
                      transactional_ddl=False)
    attributes['guard']()
    context.run_migrations()
    attributes['guard']()
