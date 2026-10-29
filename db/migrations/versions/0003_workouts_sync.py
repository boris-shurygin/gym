"""Добавь серверные занятия, неизменяемые ревизии и квитанции без backfill."""

from alembic import context
import sqlalchemy as sa
from db.schema import ensure_table

revision = '0003_workouts_sync'
down_revision = '0002_accounts_programs'
branch_labels = None
depends_on = None


def upgrade():
    attributes = context.config.attributes
    attributes['guard']()
    table = sa.Table(attributes['prefix'] + 'workout_records', sa.MetaData(),
        sa.Column('owner', sa.Text, primary_key=True, nullable=False),
        sa.Column('key', sa.Text, primary_key=True, nullable=False),
        sa.Column('payload', sa.Text, nullable=False))
    ensure_table(attributes['engine'], table)
    attributes['guard']()


def downgrade():
    raise RuntimeError('Автоматический downgrade схемы YDB не поддерживается')
