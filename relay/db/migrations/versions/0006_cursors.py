"""cursors: how far each notification sink (and the position watcher) has got, by name. Each sink
keeps its own place, so one that is down catches up later without holding the others back.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op

from relay.db.types import UTCDateTime

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cursors',
        sa.Column('name', sa.String(80), primary_key=True),
        sa.Column('value', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('cursors')
