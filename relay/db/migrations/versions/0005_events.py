"""events: what the relay tells the user about (e.g. a position whose size drifted from the venue's,
and its repair). Kept in the database so no alert is lost while nobody watches; the notification
sinks (R4) deliver from here.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op

from relay.db.types import UTCDateTime

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('kind', sa.String(60), nullable=False),
        sa.Column('severity', sa.String(10), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('deployment_id', sa.Integer(), nullable=True),
        sa.Column('position_id', sa.Integer(), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('delivered_at', UTCDateTime(), nullable=True),
    )
    op.create_index('idx_events_created', 'events', ['created_at'])
    op.create_index('idx_events_undelivered', 'events', ['delivered_at'])


def downgrade() -> None:
    op.drop_index('idx_events_undelivered', table_name='events')
    op.drop_index('idx_events_created', table_name='events')
    op.drop_table('events')
