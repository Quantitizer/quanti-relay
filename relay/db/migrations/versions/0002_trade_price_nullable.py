"""trades.price nullable: a partial close or exit whose fill price the venue has not confirmed is
recorded with the price unknown, never a guessed one.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30
"""
import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch mode: SQLite cannot ALTER a column in place (Alembic copies the table).
    with op.batch_alter_table('trades') as batch:
        batch.alter_column('price', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('trades') as batch:
        batch.alter_column('price', existing_type=sa.Float(), nullable=False)
