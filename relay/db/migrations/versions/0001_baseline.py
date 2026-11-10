"""Baseline schema: accounts, deployments, positions, trades, signals.

Revision ID: 0001
Revises: 
Create Date: 2026-10-29 13:18:15.286739
"""
import sqlalchemy as sa
from alembic import op

import relay.db.types

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('venue', sa.String(length=30), server_default='bybit', nullable=False),
    sa.Column('demo', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('testnet', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('credentials_ref', sa.String(length=100), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('guardian_state', sa.Text(), server_default='{}', nullable=False),
    sa.Column('updated_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('signals',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('signal_id', sa.String(length=100), nullable=False),
    sa.Column('deployment', sa.String(length=50), nullable=True),
    sa.Column('kind', sa.String(length=10), nullable=True),
    sa.Column('symbol', sa.String(length=50), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('received_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'signal_id', name='uq_signal_source_id')
    )
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_signals_deployment'), ['deployment'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_received_at'), ['received_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_symbol'), ['symbol'], unique=False)

    op.create_table('deployments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('dry_run', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('updated_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deployments_account_id'), ['account_id'], unique=False)

    op.create_table('positions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('deployment_id', sa.Integer(), nullable=True),
    sa.Column('symbol', sa.String(length=50), nullable=False),
    sa.Column('side', sa.String(length=10), nullable=False),
    sa.Column('size', sa.Float(), nullable=False),
    sa.Column('entry_price', sa.Float(), nullable=False),
    sa.Column('exit_price', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('dry_run', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('opened_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('closed_at', relay.db.types.UTCDateTime(timezone=True), nullable=True),
    sa.Column('pnl', sa.Float(), nullable=True),
    sa.Column('funding', sa.Float(), nullable=True),
    sa.Column('pnl_percent', sa.Float(), nullable=True),
    sa.Column('pnl_source', sa.String(length=20), nullable=True),
    sa.Column('leverage', sa.Float(), nullable=True),
    sa.Column('unrealized_pnl', sa.Float(), nullable=True),
    sa.Column('close_order_id', sa.String(length=100), nullable=True),
    sa.Column('initial_stop_price', sa.Float(), nullable=True),
    sa.Column('entry_meta', sa.JSON(), nullable=True),
    sa.Column('created_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("side IN ('LONG', 'SHORT')", name='check_position_side'),
    sa.CheckConstraint("status IN ('OPEN', 'CLOSED', 'CLOSING')", name='check_position_status'),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('close_order_id')
    )
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.create_index('idx_position_closed_at', ['closed_at'], unique=False)
        batch_op.create_index('idx_position_opened_at', ['opened_at'], unique=False)
        batch_op.create_index('idx_position_status', ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_positions_deployment_id'), ['deployment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_positions_symbol'), ['symbol'], unique=False)

    # One OPEN row per (deployment, symbol, side, dry_run). Expression + partial index: written by hand
    # because autogenerate can't compare expression indexes.
    op.create_index(
        'uq_open_position_deployment_symbol_side', 'positions',
        [sa.text('coalesce(deployment_id, -1)'), 'symbol', 'side', 'dry_run'],
        unique=True,
        sqlite_where=sa.text("status = 'OPEN'"),
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    op.create_table('trades',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=True),
    sa.Column('symbol', sa.String(length=50), nullable=False),
    sa.Column('side', sa.String(length=10), nullable=False),
    sa.Column('qty', sa.Float(), nullable=False),
    sa.Column('price', sa.Float(), nullable=False),
    sa.Column('time', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('order_id', sa.String(length=100), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('trade_type', sa.String(length=20), nullable=True),
    sa.Column('stop_loss_price', sa.Float(), nullable=True),
    sa.Column('stop_loss_order_id', sa.String(length=100), nullable=True),
    sa.Column('take_profit_price', sa.Float(), nullable=True),
    sa.Column('take_profit_order_id', sa.String(length=100), nullable=True),
    sa.Column('exit_reason', sa.String(length=50), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', relay.db.types.UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint("side IN ('BUY', 'SELL')", name='check_trade_side'),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trades_exit_reason'), ['exit_reason'], unique=False)
        batch_op.create_index(batch_op.f('ix_trades_order_id'), ['order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_trades_position_id'), ['position_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_trades_stop_loss_order_id'), ['stop_loss_order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_trades_symbol'), ['symbol'], unique=False)
        batch_op.create_index(batch_op.f('ix_trades_time'), ['time'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trades_time'))
        batch_op.drop_index(batch_op.f('ix_trades_symbol'))
        batch_op.drop_index(batch_op.f('ix_trades_stop_loss_order_id'))
        batch_op.drop_index(batch_op.f('ix_trades_position_id'))
        batch_op.drop_index(batch_op.f('ix_trades_order_id'))
        batch_op.drop_index(batch_op.f('ix_trades_exit_reason'))

    op.drop_table('trades')
    op.drop_index('uq_open_position_deployment_symbol_side', table_name='positions')
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_positions_symbol'))
        batch_op.drop_index(batch_op.f('ix_positions_deployment_id'))
        batch_op.drop_index('idx_position_status')
        batch_op.drop_index('idx_position_opened_at')
        batch_op.drop_index('idx_position_closed_at')

    op.drop_table('positions')
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_deployments_account_id'))

    op.drop_table('deployments')
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_signals_symbol'))
        batch_op.drop_index(batch_op.f('ix_signals_status'))
        batch_op.drop_index(batch_op.f('ix_signals_received_at'))
        batch_op.drop_index(batch_op.f('ix_signals_deployment'))

    op.drop_table('signals')
    op.drop_table('accounts')
