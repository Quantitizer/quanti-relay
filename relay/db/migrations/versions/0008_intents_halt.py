"""Two things that must survive a crash or a restart:
- order_intents: the stop an entry was sent with, written before the order goes out, so a position
  whose fill was never recorded (the process died) is adopted with its stop, not left without one;
- deployments.halted_reason: an emergency stop stays in force until the owner resumes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op

from relay.db.types import UTCDateTime

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'order_intents',
        sa.Column('order_link', sa.String(64), primary_key=True),
        sa.Column('deployment_id', sa.Integer(), nullable=True),
        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('side', sa.String(10), nullable=False),
        sa.Column('stop_price', sa.Float(), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
    )
    with op.batch_alter_table('deployments') as batch:
        batch.add_column(sa.Column('halted_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('deployments') as batch:
        batch.drop_column('halted_reason')
    op.drop_table('order_intents')
