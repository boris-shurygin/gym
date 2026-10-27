"""Создай служебные метаданные; частичный DDL можно повторить после проверки."""

from alembic import context
import sqlalchemy as sa
from db.schema import ensure_table

revision = '0001_runtime_metadata'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    attributes = context.config.attributes
    attributes['guard']()
    # Зафиксируй DDL этой ревизии независимо от будущей текущей модели.
    runtime = sa.Table(attributes['prefix'] + 'runtime_metadata', sa.MetaData(),
                       sa.Column('key', sa.Text, primary_key=True, nullable=False),
                       sa.Column('value', sa.Text))
    ensure_table(attributes['engine'], runtime)
    # Только после describe фактической схемы Alembic запишет ревизию.
    attributes['guard']()


def downgrade():
    raise RuntimeError('Автоматический downgrade схемы YDB не поддерживается')
