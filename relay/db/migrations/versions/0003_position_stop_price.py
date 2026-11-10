"""positions.stop_price: the position's CURRENT stop (set at entry, moved by the user or a stop
rule, or read from the venue). initial_stop_price keeps the stop it opened with.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30
"""
import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


# Plain ADD/DROP COLUMN, not batch mode: on SQLite batch mode rebuilds the table and would lose the
# partial unique index on open positions (an expression index it cannot reflect).
def upgrade() -> None:
    op.add_column('positions', sa.Column('stop_price', sa.Float(), nullable=True))
    op.execute("UPDATE positions SET stop_price = initial_stop_price")


def downgrade() -> None:
    op.execute("ALTER TABLE positions DROP COLUMN stop_price")
