"""events: an index for the position watcher (has this position's opened/closed been reported?),
and the unused delivered_at column dropped (each sink keeps its own cursor instead).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op

from relay.db.types import UTCDateTime

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('idx_events_position_kind', 'events', ['position_id', 'kind'])
    op.drop_index('idx_events_undelivered', table_name='events')
    with op.batch_alter_table('events') as batch:
        batch.drop_column('delivered_at')


def downgrade() -> None:
    with op.batch_alter_table('events') as batch:
        batch.add_column(sa.Column('delivered_at', UTCDateTime(), nullable=True))
    op.create_index('idx_events_undelivered', 'events', ['delivered_at'])
    op.drop_index('idx_events_position_kind', table_name='events')
