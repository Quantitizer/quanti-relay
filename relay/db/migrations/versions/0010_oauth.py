"""oauth_clients, oauth_tokens: the chat apps the owner connected to the MCP endpoint, and their
tokens (only their SHA-256).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op

from relay.db.types import UTCDateTime

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'oauth_clients',
        sa.Column('client_id', sa.String(40), primary_key=True),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('redirect_uris', sa.JSON(), nullable=False),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('revoked_at', UTCDateTime(), nullable=True),
    )
    op.create_table(
        'oauth_tokens',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('client_id', sa.String(40), sa.ForeignKey('oauth_clients.client_id'), nullable=False),
        sa.Column('scope', sa.String(40), nullable=False),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('expires_at', UTCDateTime(), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('idx_oauth_tokens_client', 'oauth_tokens', ['client_id'])


def downgrade() -> None:
    op.drop_index('idx_oauth_tokens_client', table_name='oauth_tokens')
    op.drop_table('oauth_tokens')
    op.drop_table('oauth_clients')
