"""Добавь аккаунты/сессии и неизменяемые версии программ без backfill."""

from alembic import context
import sqlalchemy as sa
from db.schema import ensure_table

revision = '0002_accounts_programs'
down_revision = '0001_runtime_metadata'
branch_labels = None
depends_on = None


def upgrade():
    attributes = context.config.attributes
    # Схема ревизии зафиксирована здесь, не зависит от будущих runtime-моделей.
    for name in ('admissions', 'login_transactions', 'sessions', 'login_requests', 'program_records'):
        attributes['guard']()
        table = sa.Table(attributes['prefix'] + name, sa.MetaData(),
            sa.Column('owner', sa.Text, primary_key=True, nullable=False),
            sa.Column('key', sa.Text, primary_key=True, nullable=False),
            sa.Column('payload', sa.Text, nullable=False))
        ensure_table(attributes['engine'], table)
    attributes['guard']()


def downgrade():
    raise RuntimeError('Автоматический downgrade схемы YDB не поддерживается')
