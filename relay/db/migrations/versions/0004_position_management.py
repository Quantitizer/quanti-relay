"""positions.management: the stop management the user chose for a position (trailing stop; later
break-even and the take-profit ladder) and its state. NULL = none.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


# Plain ADD/DROP COLUMN, not batch mode (see 0003: batch mode would lose the partial unique index).
def upgrade() -> None:
    op.add_column('positions', sa.Column('management', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE positions DROP COLUMN management")
