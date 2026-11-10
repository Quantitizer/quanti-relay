"""Relay schema — dialect-neutral SQLAlchemy models (SQLite default, Postgres optional).

Exchange secrets are never stored here: an account's `credentials_ref` names the environment
variables that hold its keys (`{REF}_API_KEY` / `{REF}_API_SECRET`); NULL means the process-wide
venue keys.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from relay.db.types import UTCDateTime


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, onupdate=_now, nullable=False
    )


class Account(Base):
    """One exchange account (a set of keys on one venue)."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    venue: Mapped[str] = mapped_column(String(30), nullable=False, server_default="bybit")
    demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    # Env-var prefix for this account's keys — NOT a key. NULL => the process-wide venue keys.
    credentials_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    # Persisted drawdown-guardian state (halt / halt_type / day / intraday_peak), so a lifetime
    # max-DD halt survives a restart.
    guardian_state: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now, onupdate=_now)


class Deployment(Base):
    """A named deployment on an account. Owns its positions."""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    # Set by the emergency stop, cleared only by the owner (relay resume): a restart keeps it.
    halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now, onupdate=_now)


class OrderIntent(Base):
    """The stop an entry order was sent with, written before the order goes out. A position whose
    fill was never recorded (the process died in between) is adopted with this stop."""

    __tablename__ = "order_intents"

    order_link: Mapped[str] = mapped_column(String(64), primary_key=True)
    deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)


class VenueCheck(Base):
    """A run of verify-venue started from the console: running, passed or failed, with its steps."""

    __tablename__ = "venue_checks"
    __table_args__ = (Index("idx_venue_checks_exchange", "exchange", "market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(30), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    environment: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)    # running, passed, failed, interrupted
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class OAuthClient(Base):
    """A chat app the owner may connect to the MCP endpoint (relay.api.oauth): registered by the app
    itself, usable only after the owner approved it with the API key. Removed = revoked_at set."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class OAuthToken(Base):
    """An access or refresh token, kept only as the SHA-256 of the token itself."""

    __tablename__ = "oauth_tokens"
    __table_args__ = (Index("idx_oauth_tokens_client", "client_id"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)          # access, refresh
    client_id: Mapped[str] = mapped_column(String(40), ForeignKey("oauth_clients.client_id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


class Position(Base, TimestampMixin):
    """An open or closed position. One OPEN row per (deployment, symbol, side, dry_run).

    `dry_run` rows are paper positions: the relay simulated the fills and nothing exists on the
    exchange. Reconcilers, importers, the live feed and the PnL resolvers never touch them.
    """

    __tablename__ = "positions"
    __table_args__ = (
        Index("idx_position_status", "status"),
        Index("idx_position_opened_at", "opened_at"),
        Index("idx_position_closed_at", "closed_at"),
        # COALESCE(deployment_id, -1) so rows with no deployment still collide with each other
        # (NULLs are otherwise distinct in a unique index). dry_run is part of the key so a paper
        # position never blocks (or is blocked by) a real one.
        Index(
            "uq_open_position_deployment_symbol_side",
            text("coalesce(deployment_id, -1)"), "symbol", "side", "dry_run",
            unique=True,
            sqlite_where=text("status = 'OPEN'"),
            postgresql_where=text("status = 'OPEN'"),
        ),
        CheckConstraint("side IN ('LONG', 'SHORT')", name="check_position_side"),
        CheckConstraint("status IN ('OPEN', 'CLOSED', 'CLOSING')", name="check_position_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # LONG / SHORT
    size: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Realized trade PnL incl. trading fees, EXCLUDING funding (see `funding`).
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Cumulative funding paid (-) / received (+) over the hold; NULL = not yet reconciled.
    funding: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_percent: Mapped[float | None] = mapped_column(Float, nullable=True)  # % of the position's value at entry
    # How PnL was determined (relay.execution.close_record): venue_api (the venue's complete records,
    # the only verified source), local_calc (a relay-placed close priced from its real fills, without
    # fees/funding), pending (not known yet: exit_price and pnl are NULL until the reconciler prices
    # it), dry_run (a paper position's simulated result).
    pnl_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    leverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Venue order id of the closing trade (dedup).
    close_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    initial_stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The CURRENT fixed stop: set at entry, moved by the user (or a stop rule), or read from the
    # venue by the position sync. The stop audit re-arms this one. A trailing stop is separate (it
    # never changes this one; see `management`).
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Stop management the user chose for this position and its state, e.g. {"trailing":
    # {"distance_pct", "activate_pct", "distance", "active_price", "set_at"}}. NULL = none.
    management: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Free-form context recorded at entry (signal id, source, requested stop/size, ...).
    entry_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    trades: Mapped[list[Trade]] = relationship(back_populates="position", cascade="all, delete-orphan")


class Trade(Base, TimestampMixin):
    """One execution (entry, exit, partial) belonging to a position."""

    __tablename__ = "trades"
    __table_args__ = (CheckConstraint("side IN ('BUY', 'SELL')", name="check_trade_side"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY / SELL
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # NULL = fill not confirmed
    time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trade_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # entry/exit/partial/import
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    position: Mapped[Position | None] = relationship(back_populates="trades")


class Event(Base):
    """Something the relay tells the user about (relay.events). Each notification sink keeps its
    place in this table by id (Cursor)."""

    __tablename__ = "events"
    __table_args__ = (Index("idx_events_created", "created_at"), Index("idx_events_position_kind", "position_id", "kind"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)        # e.g. position.drift_repaired
    severity: Mapped[str] = mapped_column(String(10), nullable=False)    # info / warning / critical
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)


class Cursor(Base):
    """How far a named reader has got (a notification sink: the last event id it handled)."""

    __tablename__ = "cursors"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now, onupdate=_now)


class Signal(Base):
    """The signal log: every received signal, its verdict and its execution result."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("source", "signal_id", name="uq_signal_source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_id: Mapped[str] = mapped_column(String(100), nullable=False)  # sender's idempotency key
    deployment: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    kind: Mapped[str | None] = mapped_column(String(10), nullable=True)  # event / target
    symbol: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    # accepted (received, being processed) / executed (an order filled; `reasons` notes an outcome such
    # as a stop that could not be set) / skipped (refused; `reasons` says why)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now, onupdate=_now)
