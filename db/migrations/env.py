"""Конфигурация Alembic для проверенных DDL без фиктивной общей транзакции."""

from alembic import context
from alembic.ddl.impl import DefaultImpl
import sqlalchemy as sa


class YDBImpl(DefaultImpl):
    __dialect__ = 'yql'
    transactional_ddl = False

    def version_table_impl(self, *, version_table, version_table_schema, version_table_pk, **kw):
        # Публичный hook Alembic; строковый PK, без autoincrement/sequence.
        return sa.Table(version_table, sa.MetaData(),
                        sa.Column('version_num', sa.Text, primary_key=True, nullable=False))


attributes = context.config.attributes
with attributes['engine'].connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
    context.configure(connection=connection,
                      version_table=attributes['prefix'] + 'alembic_version',
                      transactional_ddl=False)
    attributes['guard']()
    context.run_migrations()
    attributes['guard']()
