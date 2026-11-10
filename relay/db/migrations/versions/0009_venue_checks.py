"""venue_checks: the runs of relay verify-venue started from the console, so going live can ask
whether this exchange's connector passed on a test account.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op

from relay.db.types import UTCDateTime

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'venue_checks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('exchange', sa.String(30), nullable=False),
        sa.Column('market', sa.String(10), nullable=False),
        sa.Column('environment', sa.String(10), nullable=False),
        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('steps', sa.JSON(), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('finished_at', UTCDateTime(), nullable=True),
    )
    op.create_index('idx_venue_checks_exchange', 'venue_checks', ['exchange', 'market'])


def downgrade() -> None:
    op.drop_index('idx_venue_checks_exchange', table_name='venue_checks')
    op.drop_table('venue_checks')
