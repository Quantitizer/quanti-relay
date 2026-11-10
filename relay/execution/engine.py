"""
Execution Engine - turns an accepted trade instruction into exchange orders and keeps the book.

Integrates:
- RiskManager (pre-trade checks)
- compute_size (position sizing)
- OrderExecutor (market orders, reduce-only closes)
- StopLossManager (exchange-side stops)
- PositionTracker (reconciliation + import)

One engine serves one deployment on one exchange account. Entry, exit, partial exit, fill handling
from the private WebSocket, and the background loops (position sync, stop audit) live here; deciding
WHAT to trade does not — the engine executes instructions it is given.

Dry-run: every order and stop goes through the deployment's dry-run flag. A dry-run position is a
PAPER position (positions.dry_run): its fills are simulated, it never touches the exchange, and the
reconcilers leave it alone.
"""
import asyncio
import contextlib
import json
import math
import time as _time
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from relay.exchange.bybit.rest_client import BybitRestClient, is_benign_order_state
from relay.execution.close_record import price_close, write_close
from relay.execution.dry_run import DryRunFlag, resolve_dry_run
from relay.execution.error_messages import humanize_order_error
from relay.execution.order_executor import OrderExecutor
from relay.execution.order_link import make_order_link
from relay.execution.position_sizer import compute_size
from relay.execution.position_tracker import IMPORT_TRADE_REASON, PositionTracker
from relay.execution.risk_manager import RiskManager
from relay.execution.rounding import (
    format_price,
    format_qty,
    round_price_to_tick,
    round_qty_to_step,
    round_stop_to_tick,
)
from relay.execution.stop_loss_manager import StopLossManager
from relay.execution.take_profit_ladder import ladder_price_problem, ladder_problem, plan_ladder
from relay.rails import notional_cap, outside_trading_hours, stop_past_bankruptcy, symbol_refusal
from relay.signals.contract import unified_symbol
from relay.venue import venue_of


def _in_list(start: int, values) -> str:
    """`$n, $n+1, ...` placeholders for an IN (...) list; 'NULL' when empty (matches nothing)."""
    return ", ".join(f"${i}" for i in range(start, start + len(values))) or "NULL"



# How long a close may stay owned by the relay before the background closers ignore the claim.
CLOSE_OWNERSHIP_S = 300

# How long the venue's lot/tick rules are reused before being read again (engineering constant).
INSTRUMENT_CACHE_S = 3600

def _position_mode(position: dict[str, Any]) -> bool | None:
    """A position's own mode for closing it: True = paper, False = real, None = unknown (the
    executor's current flag decides). A close must follow the POSITION, never a flag that may
    have been switched since it opened."""
    if 'dry_run' not in position or position['dry_run'] is None:
        return None
    return bool(position['dry_run'])


# Venue order states that mean the order no longer rests on the book.
# A size difference is repaired no sooner than this many stop-rules intervals after it was
# first seen (the ladder records its fills on that pass, so a fill is never mistaken for drift).
DRIFT_MIN_AGE_RULE_PASSES = 2
# A background loop counts as stuck when it has not finished a round for twice its wait plus this
# long (a round with many positions and slow venue answers can take minutes).
LOOP_STUCK_GRACE_S = 600
_ORDER_DONE = ('Filled', 'Cancelled', 'Rejected', 'Deactivated', 'PartiallyFilledCanceled')


def _json_obj(value) -> dict:
    """A JSON column read on the raw path (SQLite returns text) as a dict; {} when empty."""
    if isinstance(value, str):
        value = json.loads(value) if value else None
    return dict(value) if isinstance(value, dict) else {}


def trailing_problem(distance_pct, activate_pct) -> str | None:
    """Why these trailing-stop inputs are unusable, or None (distance_pct None/0 = no trailing)."""
    for name, v in (('trailing distance', distance_pct), ('trailing activation', activate_pct)):
        if v is not None and not (isinstance(v, (int, float)) and math.isfinite(v)):
            return f'{name} must be a number'
    if distance_pct and not 0 < distance_pct < 100:
        return 'trailing distance must be between 0 and 100 %'
    if activate_pct is not None and activate_pct < 0:
        return 'trailing activation must be 0 % or more'
    if activate_pct and not distance_pct:
        return 'a trailing activation needs a trailing distance'
    return None


def break_even_problem(trigger_pct, trigger_r, buffer_pct, trigger_tp=None) -> str | None:
    """Why these break-even inputs are unusable, or None (no trigger and no buffer = no break-even).
    trigger_tp n = once take-profit level n has filled."""
    for name, v in (('break-even trigger %', trigger_pct), ('break-even trigger R', trigger_r),
                    ('break-even buffer', buffer_pct)):
        if v is not None and not (isinstance(v, (int, float)) and math.isfinite(v)):
            return f'{name} must be a number'
    if trigger_tp is not None and (isinstance(trigger_tp, bool) or not isinstance(trigger_tp, int)
                                   or trigger_tp < 1):
        return 'the break-even take-profit trigger must be a level number (1 = the first level)'
    if sum(t is not None for t in (trigger_pct, trigger_r, trigger_tp)) > 1:
        return 'set one break-even trigger: a profit %, an R multiple or a take-profit level'
    trigger = trigger_pct if trigger_pct is not None else trigger_r
    if trigger is not None and trigger <= 0:
        return 'the break-even trigger must be more than 0'
    if trigger is None and trigger_tp is not None:
        trigger = trigger_tp
    if buffer_pct is not None:
        if trigger is None:
            return 'a break-even buffer needs a break-even trigger'
        if buffer_pct < 0:
            return 'the break-even buffer must be 0 % or more'
        if trigger_pct is not None and buffer_pct >= trigger_pct:
            return 'the break-even buffer must be smaller than its trigger (the stop would be at or past the price)'
    return None


def break_even_levels(side: str, entry: float, initial_stop: float | None, plan: dict[str, Any],
                      tick: float | None) -> tuple[float | None, float]:
    """(trigger price, stop target) of a break-even plan; the trigger price is None for a
    take-profit-level trigger. Raises ValueError when it cannot work: an R trigger without an
    opening stop, or a target not strictly between entry and trigger."""
    long_side = side.lower() == 'long'
    sign = 1 if long_side else -1
    if plan.get('trigger_tp') is not None:
        return None, round_stop_to_tick(entry * (1 + sign * (plan.get('buffer_pct') or 0) / 100), tick, side)
    if plan.get('trigger_r') is not None:
        if not initial_stop or (initial_stop >= entry if long_side else initial_stop <= entry):
            raise ValueError('a break-even trigger in R needs the opening stop on the loss side of entry')
        trigger = entry + sign * plan['trigger_r'] * abs(entry - initial_stop)
    else:
        trigger = entry * (1 + sign * plan['trigger_pct'] / 100)
    target = entry * (1 + sign * (plan.get('buffer_pct') or 0) / 100)
    trigger = round_price_to_tick(trigger, tick)
    if trigger <= 0:
        raise ValueError(f'the break-even trigger would be at {trigger:g}: a price must stay above 0')
    target = round_stop_to_tick(target, tick, side)
    if not (target < trigger if long_side else target > trigger):
        raise ValueError(f'the break-even stop {target:g} would not be below its trigger {trigger:g}'
                         if long_side else
                         f'the break-even stop {target:g} would not be above its trigger {trigger:g}')
    return trigger, target


class ExecutionEngine:
    """
    Main execution engine orchestrator.

    Coordinates all execution components for end-to-end trade execution.
    """

    def __init__(
        self,
        db_pool,
        bybit_client: BybitRestClient,
        settings_provider,
        dry_run: DryRunFlag = True,
        deployment_id: int | None = None,
        signal_log: Any | None = None,
        owned_deployment_ids: list | None = None,
    ):
        """
        Initialize execution engine.

        Args:
            db_pool: Database (relay.db.Database)
            bybit_client: Bybit REST client of this deployment's account
            settings_provider: dict, or (async) callable returning a dict, of execution settings:
                leverage, isolated_margin, require_stop, min_stop_distance_pct, max_stop_distance_pct,
                take_profit_pct, cooldown_hours, max_positions, max_exposure_pct, daily_loss_limit_pct,
                min_balance, max_size_shortfall_pct, position_sync_interval_s, stop_audit_interval_s.
                None/absent = that check is off; no trading number has a default.
            dry_run: the deployment's dry-run flag: bool or (async) callable, read on every order
            deployment_id: the deployment that owns the positions this engine opens
            signal_log: optional with `async mark_skipped(signal_id, reason)` /
                `async mark_executed(signal_id)`
            owned_deployment_ids: when this engine's private WS is on a SPECIFIC account's key, pass
                that account's deployment ids so the realtime close/unrealized writers only touch ITS
                rows. None (default) = the default account's rows only.
        """
        self.db = db_pool
        self.bybit = bybit_client
        self._settings_provider = settings_provider
        self._dry_run = dry_run
        self.deployment_id = deployment_id
        self._ws_owned_deployment_ids = list(owned_deployment_ids) if owned_deployment_ids else None
        self.signal_log = signal_log
        self.private_ws = None  # Will be set via set_private_ws()

        # Will be initialized after settings are loaded
        self.risk_manager: RiskManager | None = None
        self.order_executor: OrderExecutor | None = None
        self.stop_loss_manager: StopLossManager | None = None
        self.position_tracker: PositionTracker | None = None

        # State
        self._running = False
        # Set by an emergency stop: every later entry is refused until the relay is restarted.
        self.halted_reason: str | None = None
        self._last_exit_time: dict[str, datetime] = {}
        self._limits_lock = asyncio.Lock()
        self._instrument_cache: dict[str, dict[str, Any]] = {}
        self._tasks: list[asyncio.Task] = []
        self._loop_names: list[str] = []
        # Per loop: when it last finished a round, and how long it then waits (stuck_loops)
        self._beats: dict[str, tuple[float, float]] = {}

        # Real-time position tracking stats
        self._realtime_position_updates = 0
        self._realtime_order_updates = 0

        # Per-symbol lock to prevent double partial exits from concurrent trigger paths
        self._reduce_locks: dict[int, asyncio.Lock] = {}
        # Positions the relay is closing right now: id -> when it took the close (see _own_close).
        self._closing: dict[int, float] = {}
        # Positions whose time-stop close failed and was reported (reported again only after a success).
        self._time_stop_failed: set[int] = set()
        # Entries being executed right now, as futures resolved when each ends (an emergency stop
        # waits for them).
        self._entries_in_flight: set[asyncio.Future] = set()
        # Per-symbol lock for entry (two signals for one symbol must not both open)
        self._entry_locks: dict[str, asyncio.Lock] = {}
        # One stop-rules pass at a time (the loop and the API's run-now)
        self._stop_rules_lock = asyncio.Lock()
        # Size drift between the record and the venue (repair_drift): {position id: ((recorded,
        # venue, pending ladder fills), clock time first seen)}, and the differences already
        # reported as not repairable.
        self._drift_seen: dict[int, tuple[tuple[float, float, float], float]] = {}
        self._drift_unrepairable: dict[int, float] = {}
        self._clock = _time.monotonic
        self.events = None          # relay.events.EventLog, set by the runtime (None: logged only)
        self._unwatched_reported = 0  # live rows without keys last reported (said again when it changes)
        self._naked_reported: set[str] = set()   # symbols reported as open without a stop

    async def get_dry_run_mode(self) -> bool:
        """The deployment's dry-run flag, read now. Fails safe (True) if it can't be read."""
        try:
            return await resolve_dry_run(self._dry_run)
        except Exception as e:
            logger.warning(f"[ExecutionEngine] Failed to read dry-run flag (assuming dry-run): {e}")
            return True

    def set_private_ws(self, private_ws: Any) -> None:
        """
        Set the private WebSocket client for real-time position/order updates.

        This enables instant position sync instead of polling:
        - Position updates: size changes, PnL updates, closures
        - Order updates: fills, rejects, stop-loss triggers

        Args:
            private_ws: BybitPrivateWsClient instance
        """
        self.private_ws = private_ws

        # Register handlers for real-time updates
        private_ws.register_position_handler(self._on_realtime_position_update)
        private_ws.register_order_handler(self._on_realtime_order_update)

        logger.info("[ExecutionEngine] Private WebSocket connected for real-time position sync")

    def _account_scope_sql(self, start: int) -> tuple[str, list]:
        """WHERE fragment (+ its params from $start) limiting a query to this account's LIVE rows:
        the owned deployments when set, else rows with no deployment or a default-account deployment.
        Paper rows are never touched by exchange events."""
        if self._ws_owned_deployment_ids is not None:
            ids = self._ws_owned_deployment_ids
            return f" AND NOT dry_run AND deployment_id IN ({_in_list(start, ids)})", list(ids)
        return (
            (" AND NOT dry_run AND (deployment_id IS NULL OR deployment_id IN ("
             "SELECT d.id FROM deployments d JOIN accounts a ON a.id = d.account_id "
             f"WHERE a.venue = '{venue_of(self.bybit)}' AND a.credentials_ref IS NULL))"),
            [],
        )

    def _risk_scope_sql(self, start: int, is_dry_run: bool) -> tuple[str, list]:
        """Scope of the risk checks: paper trades see this deployment's paper rows; live trades see
        every live row on this account (see relay.execution.risk_manager)."""
        if is_dry_run:
            return f" AND dry_run AND coalesce(deployment_id, -1) = coalesce(${start}, -1)", [self.deployment_id]
        return self._account_scope_sql(start)

    async def _on_realtime_position_update(self, position) -> None:
        """
        Handle real-time position updates from WebSocket.

        This provides instant updates instead of polling:
        - Position closed (size=0): Mark as CLOSED
        - Position open: Update unrealized PnL in database

        Args:
            position: PositionUpdate from BybitPrivateWsClient
        """
        self._realtime_position_updates += 1
        symbol = position.symbol
        size = position.size

        try:
            if size is None:
                logger.debug(f"[RealtimeSync] {symbol}: update without a size - ignored")
            elif size == 0:
                # Position CLOSED - handle immediately
                await self._handle_realtime_position_closed(symbol, position)
            else:
                # Position still open - update PnL
                await self._handle_realtime_position_update(symbol, position)

        except Exception as e:
            logger.error(f"[RealtimeSync] Error handling position update for {symbol}: {e}")

    async def _handle_realtime_position_closed(self, symbol: str, position) -> None:
        """
        A position closed on the venue (stop hit, liquidation, manual close), seen on the live feed.
        Recorded like every close (relay.execution.close_record): the venue's complete records if
        already published, else 'pending' for the reconciler. The cause is not guessed: the exit
        reason is 'closed_on_venue'.
        """
        logger.info(f"[RealtimeSync] Position CLOSED detected: {symbol} (via WebSocket)")
        try:
            async with self.db.acquire() as conn:
                # Account-scoped: a shared/misrouted stream can't close another account's
                # same-symbol position, and never a paper position.
                scope_sql, scope_args = self._account_scope_sql(2)
                row = await conn.fetchrow(
                    "SELECT id, symbol, side, entry_price, size, opened_at, entry_meta FROM positions "
                    "WHERE symbol = $1 AND status = 'OPEN'" + scope_sql +
                    " ORDER BY opened_at DESC LIMIT 1",
                    symbol, *scope_args
                )
            if not row:
                logger.debug(f"[RealtimeSync] {symbol}: No matching open position in database")
                return
            row = dict(row)
            if self._close_owned(row['id']):
                return   # the relay is closing it and records the result itself (with the real fill)
            closed_at = datetime.now(UTC)
            priced = await price_close(self.db, self.bybit, row, closed_at)
            async with self.db.acquire() as conn:
                if not await write_close(conn, row, priced, closed_at, 'closed_on_venue'):
                    return   # already recorded by another writer
            logger.info(f"[RealtimeSync] ✓ {symbol} #{row['id']} marked CLOSED: "
                        f"PnL {priced['pnl']} ({priced['pnl_source']})")
        except Exception as e:
            logger.error(f"[RealtimeSync] Error handling position closure for {symbol}: {e}")

    async def _handle_realtime_position_update(self, symbol: str, position) -> None:
        """
        Handle real-time PnL update for open position.

        Updates unrealized PnL in database instantly instead of waiting for the sync loop.
        """
        try:
            async with self.db.acquire() as conn:
                # Update unrealized PnL for matching open position (account-scoped, never paper rows)
                scope_sql, scope_args = self._account_scope_sql(4)
                await conn.execute(
                    "UPDATE positions SET unrealized_pnl = COALESCE($1, unrealized_pnl), updated_at = $2 "
                    "WHERE symbol = $3 AND status = 'OPEN'" + scope_sql,
                    position.unrealized_pnl,
                    datetime.now(UTC),
                    symbol,
                    *scope_args,
                )

                # Log significant PnL changes (every 100th update to avoid spam)
                if self._realtime_position_updates % 100 == 0:
                    logger.debug(
                        f"[RealtimeSync] {symbol}: PnL={position.unrealized_pnl} "
                        f"(update #{self._realtime_position_updates})"
                    )

        except Exception as e:
            logger.error(f"[RealtimeSync] Error updating PnL for {symbol}: {e}")

    async def _on_realtime_order_update(self, order) -> None:
        """
        Handle real-time order updates from WebSocket.

        Logs fills, rejections and cancellations (stop-loss triggers and manual closes arrive as
        fills).

        Args:
            order: OrderUpdate from BybitPrivateWsClient
        """
        self._realtime_order_updates += 1

        try:
            symbol = order.symbol
            status = order.status.value if hasattr(order.status, 'value') else str(order.status)

            # Log significant order events
            if status in ['Filled', 'Cancelled', 'Rejected']:
                logger.info(
                    f"[RealtimeSync] Order {status}: {symbol} "
                    f"{order.side} {order.qty} @ {order.price or 'MARKET'} "
                    f"(filled: {order.filled_qty})"
                )

        except Exception as e:
            logger.error(f"[RealtimeSync] Error handling order update: {e}")

    async def initialize(self) -> None:
        """Initialize all components with settings."""
        settings = await self._get_execution_settings()

        self.risk_manager = RiskManager(
            db_pool=self.db,
            bybit_client=self.bybit,
            settings_manager=self,
            min_balance=settings.get('min_balance'),
            dry_run=self._dry_run,
            scope=self._risk_scope_sql,
        )

        self.order_executor = OrderExecutor(
            bybit_client=self.bybit,
            dry_run=self._dry_run,
        )

        self.stop_loss_manager = StopLossManager(
            bybit_client=self.bybit,
            dry_run=self._dry_run,
        )

        self.position_tracker = PositionTracker(
            db_pool=self.db,
            bybit_client=self.bybit,
            default_leverage=float(settings['leverage']) if settings.get('leverage') else None,
            busy=self._close_owned,
        )

        dry_run_mode = await self.get_dry_run_mode()
        logger.info(
            f"[ExecutionEngine] Initialized in {'DRY-RUN' if dry_run_mode else 'LIVE'} mode "
            f"(deployment={self.deployment_id})"
        )

    async def _get_execution_settings(self) -> dict[str, Any]:
        """The deployment's execution settings, read now."""
        provider = self._settings_provider
        value = provider() if callable(provider) else provider
        if asyncio.iscoroutine(value):
            value = await value
        return dict(value or {})

    async def get_execution_settings(self) -> dict[str, Any]:
        """Settings-manager interface used by RiskManager."""
        return await self._get_execution_settings()

    async def _unconfirmed_entry_price(self, symbol: str, direction: str, quantity: float,
                                       expected_entry: float) -> tuple:
        """Entry price when the venue accepted the order but could not confirm the fill in time.
        The venue's position average is used when that position is ours alone (its size equals
        this fill); otherwise the expected price, marked 'expected_unconfirmed' so it is never
        mistaken for a venue fact. Returns (price, source)."""
        try:
            want = 'Buy' if direction == 'long' else 'Sell'
            mine = [p for p in await self.bybit.get_positions(category='linear', symbol=symbol)
                    if p.get('side') == want and float(p.get('size') or 0) > 0]
            if len(mine) == 1 and abs(float(mine[0]['size']) - quantity) < 1e-12 and float(mine[0].get('avgPrice') or 0) > 0:
                logger.warning(f"[Entry] {symbol}: fill unconfirmed - using the venue's position average price")
                return float(mine[0]['avgPrice']), 'venue_position'
        except Exception as e:
            logger.warning(f"[Entry] {symbol}: position lookup for the unconfirmed fill failed: {e}")
        logger.error(f"[Entry] {symbol}: fill UNCONFIRMED - recording the expected price {expected_entry} "
                     f"(marked expected_unconfirmed; the PnL reconciler uses the venue's record later)")
        return expected_entry, 'expected_unconfirmed'

    async def _annotate_signal(self, signal_id, outcome: str) -> None:
        """Record a post-execution outcome (e.g. the stop failed and the position was closed) on the
        signal row, so the log tells the whole story, not just 'executed'."""
        if signal_id and self.signal_log is not None and hasattr(self.signal_log, 'mark_outcome'):
            try:
                await self.signal_log.mark_outcome(signal_id, outcome)
            except Exception as e:
                logger.error(f"[Entry] failed to record outcome {outcome} on signal {signal_id}: {e}")

    async def _instrument_specs(self, symbol: str) -> dict[str, float] | None:
        """qty_step / min_qty / max_qty / tick_size for a linear contract. Cached per symbol for
        INSTRUMENT_CACHE_S (venues do change lot and tick rules; a stale rule gets orders rejected)."""
        cached = self._instrument_cache.get(symbol)
        if cached and _time.monotonic() - cached['_fetched'] < INSTRUMENT_CACHE_S:
            return cached
        rows = await self.bybit.list_instruments(category='linear', symbol=symbol)
        if not rows:
            return None
        row = rows[0]
        lot = row.get('lotSizeFilter') or {}
        pf = row.get('priceFilter') or {}
        specs = {
            # Only what the venue publishes; nothing is assumed (no step -> no order).
            'qty_step': float(lot.get('qtyStep') or 0) or None,
            'min_qty': float(lot.get('minOrderQty') or 0),
            # Market orders have their own, lower cap where the venue states one.
            'max_qty': float(lot.get('maxMktOrderQty') or lot.get('maxOrderQty') or 0) or float('inf'),
            'min_notional': float(lot.get('minNotionalValue') or 0),
            'tick_size': float(pf.get('tickSize') or 0) or None,
            '_fetched': _time.monotonic(),
        }
        self._instrument_cache[symbol] = specs
        return specs

    def account_readable(self) -> bool:
        """Whether the venue's account can be read: False on paper without exchange keys, where
        the relay only reads prices and market rules (and no live position can exist)."""
        has = getattr(self.bybit, "has_keys", None)
        if has is None:
            has = getattr(self.bybit, "api_key", True)
        return bool(has)

    async def _record_intent(self, link: str, symbol: str, direction: str, stop_price: float | None) -> None:
        """Write down the stop a live entry is sent with, before it is sent. If that fails the entry
        is not sent: a fill nobody recorded must still be adoptable with its stop."""
        async with self.db.acquire() as conn:
            await conn.execute(
                "INSERT INTO order_intents (order_link, deployment_id, symbol, side, stop_price, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                link, self.deployment_id, symbol, 'LONG' if direction == 'long' else 'SHORT', stop_price,
                datetime.now(UTC))

    async def _live_rows_without_keys(self) -> int:
        """Live positions recorded although the account cannot be read (keys removed while they
        were open): they cannot be watched, so this is said loudly."""
        async with self.db.acquire() as conn:
            rows = await conn.fetch("SELECT count(*) AS n FROM positions WHERE status IN ('OPEN', 'CLOSING') "
                                    "AND dry_run = $1 AND deployment_id = $2", False, self.deployment_id)
        n = int(rows[0]["n"]) if rows else 0
        if n:
            logger.critical(f"[ExecutionEngine] {n} live position(s) are recorded, but there are no exchange keys: "
                            f"they cannot be watched or protected - set the keys again, or check them on the venue")
        if n and n != self._unwatched_reported:            # once per change, not every round
            await self._event('position.unwatched', 'critical',
                              f"{n} live position(s) are recorded, but there are no exchange keys: they cannot be "
                              f"watched or protected - set the keys again, or check them on the venue", {'count': n})
        self._unwatched_reported = n
        return n

    async def _save_halt(self, reason: str | None) -> None:
        """Keep the emergency stop in the database, so a crash or a reboot does not resume trading.
        A failure is logged, never raised: the halt holds in memory either way."""
        try:
            async with self.db.acquire() as conn:
                await conn.execute("UPDATE deployments SET halted_reason = $1, updated_at = $2 WHERE id = $3",
                                   reason, datetime.now(UTC), self.deployment_id)
        except Exception as e:
            logger.critical(f"[ExecutionEngine] the halt could not be saved ({e}): a restart would resume trading")

    async def _load_halt(self) -> None:
        async with self.db.acquire() as conn:
            rows = await conn.fetch("SELECT halted_reason FROM deployments WHERE id = $1", self.deployment_id)
        reason = rows[0]["halted_reason"] if rows else None
        if reason:
            self.halted_reason = reason
            logger.critical(f"[ExecutionEngine] halted since an emergency stop ({reason}): new entries are refused "
                            f"until the owner resumes (console, or POST /v1/resume). Positions are still watched.")

    async def resume(self) -> dict[str, Any]:
        """The owner lifts an emergency stop: new entries are taken again, and a stopped engine starts."""
        was = self.halted_reason
        await self._save_halt(None)
        self.halted_reason = None
        if not self._running:
            await self.start()
        if was:
            await self._event('relay.resumed', 'warning', f"the owner resumed trading after {was}", {'after': was})
        return {'resumed': bool(was), 'running': self._running}

    async def start(self) -> None:
        """Start the execution engine."""
        await self.initialize()
        await self._load_halt()
        if not self.account_readable() and not await self.get_dry_run_mode():
            raise RuntimeError("live trading needs the venue's exchange keys, and none are set: set them in the "
                               "environment, or put the deployment on paper (dry_run)")
        self._running = True
        if not self.account_readable():
            logger.info("[ExecutionEngine] no exchange keys: the venue's account is not read (paper: prices and "
                        "market rules only)")
            await self._live_rows_without_keys()
            await self.position_tracker.restore_positions()
            self._start_loops(position_sync=self._position_sync_loop, stop_rules=self._stop_rules_loop)
            logger.info("[ExecutionEngine] Started")
            return

        # Restore open positions from database
        try:
            await self.position_tracker.restore_positions()
        except Exception as e:
            logger.warning(
                f"[ExecutionEngine] restore_positions failed (engine will start anyway): {e}"
            )

        # Reconcile DB positions with exchange (detect positions closed on Bybit)
        # Resilient: Bybit API timeout/auth failure should not prevent engine start
        try:
            await self.position_tracker.sync_all_positions()
        except Exception as e:
            logger.warning(
                f"[ExecutionEngine] sync_all_positions failed (engine will start anyway): {e}. "
                "Position sync will retry in background loop."
            )

        # Import any positions from Bybit that aren't in the database
        # This captures positions opened manually or before the relay started
        import_result: dict[str, Any] = {"imported_count": 0, "imported_symbols": []}
        try:
            import_result = await self.position_tracker.import_exchange_positions()
        except Exception as e:
            logger.warning(
                f"[ExecutionEngine] import_exchange_positions failed (engine will start anyway): {e}. "
                "Positions can be synced manually or on next loop."
            )
        if import_result.get('imported_count', 0) > 0:
            logger.info(
                f"[ExecutionEngine] Imported {import_result['imported_count']} positions from Bybit: "
                f"{', '.join(import_result.get('imported_symbols', []))}"
            )

        # Start background tasks
        self._start_loops(position_sync=self._position_sync_loop, stop_audit=self._stop_audit_loop,
                          stop_rules=self._stop_rules_loop)

        logger.info("[ExecutionEngine] Started")

    def _start_loops(self, **loops) -> None:
        now = _time.monotonic()
        self._loop_names = list(loops)
        self._beats = {name: (now, 0.0) for name in loops}    # a first round that hangs counts too
        self._tasks = [asyncio.create_task(loop()) for loop in loops.values()]

    @property
    def is_running(self) -> bool:
        """True while the background loops run (False after stop or an emergency stop)."""
        return self._running

    async def stop(self) -> None:
        """Stop the execution engine."""
        self._running = False
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        self._tasks = []
        for t in tasks:
            t.cancel()
        # Wait for the loops to unwind: one still inside a DB call would otherwise outlive the
        # connection pool and block its shutdown.
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[ExecutionEngine] Stopped")

    async def execute_entry(self, signal: dict[str, Any]) -> dict[str, Any]:
        """
        Execute trade entry.

        Args:
            signal: {'symbol', 'direction' ('long'/'short'), 'size' (a position_sizer size object),
                'entry_price' (expected; the ticker is used when absent), 'stop_price' or
                'stop_pct' (% from the expected entry; one is required unless the settings say
                require_stop: False), optional 'take_profit_price', 'leverage', 'signal_id'
                (signals row id), 'reason', 'dry_run' (True = a paper position even on a live
                deployment; it can only make an entry more cautious), 'flip' (the second half of
                a flip: the user's cooldown after a close does not apply)}

        Returns:
            Dict with 'success': bool, 'reason': str (if failed)
        """
        symbol = signal['symbol']
        signal_id = signal.get('signal_id')

        logger.info(
            f"[Entry] {symbol}: Executing {signal['direction']} entry"
        )

        async def _mark_signal_skipped(reason: str):
            """Helper to mark signal as skipped when execution fails."""
            if signal_id and self.signal_log is not None:
                try:
                    await self.signal_log.mark_skipped(signal_id, reason)
                except Exception as e:
                    logger.error(f"[Entry] {symbol}: Failed to mark signal {signal_id} as skipped: {e}")

        # Guard: ensure components are initialized (prevents AttributeError if initialize() failed)
        if not self.risk_manager or not self.order_executor or not self.position_tracker:
            reason = "execution_engine_not_initialized"
            logger.error(
                f"[Entry] {symbol}: Execution engine components not initialized! "
                f"risk_manager={self.risk_manager is not None}, "
                f"order_executor={self.order_executor is not None}, "
                f"position_tracker={self.position_tracker is not None}"
            )
            await _mark_signal_skipped(reason)
            return {'success': False, 'reason': reason}

        # With a limit on the whole book set (position count, exposure, the gross-leverage rail),
        # entries run one at a time: otherwise two signals for different symbols could both pass
        # the limit before either is recorded.
        settings = await self._get_execution_settings()
        limits = (settings.get('max_positions') is not None or settings.get('max_exposure_pct') is not None
                  or (settings.get('rails') or {}).get('max_gross_leverage') is not None)
        # Lock order: the limits lock (if any), then the symbol's lock. The entry is registered as in
        # flight so an emergency stop can wait for it (see close_all).
        done = asyncio.get_running_loop().create_future()
        self._entries_in_flight.add(done)
        try:
            async with (self._limits_lock if limits else contextlib.nullcontext(),
                        self._entry_locks.setdefault(symbol, asyncio.Lock())):
                return await self._execute_entry_locked(signal, _mark_signal_skipped)
        finally:
            done.set_result(None)
            self._entries_in_flight.discard(done)

    async def _execute_entry_locked(self, signal: dict[str, Any], _mark_signal_skipped) -> dict[str, Any]:
        symbol = signal['symbol']
        signal_id = signal.get('signal_id')

        if self.halted_reason:
            logger.warning(f"[Entry] {symbol}: refused - engine halted ({self.halted_reason})")
            await _mark_signal_skipped('engine_halted')
            return {'success': False, 'reason': 'engine_halted'}

        # INTENT LOG PATTERN: Track if an exchange order was placed for smart error recovery
        order_placed_on_exchange = False
        order_result = None
        quantity = None

        try:
            settings = await self._get_execution_settings()
            is_dry_run = await self.get_dry_run_mode() or signal.get('dry_run') is True

            # The deployment's own kill switch: no new entries (closes and stops still work).
            if settings.get('enabled') is False:
                await _mark_signal_skipped('deployment_disabled')
                logger.warning(f"[Entry] {symbol}: refused - the deployment is disabled (enabled: false)")
                return {'success': False, 'reason': 'deployment_disabled'}

            # Rails that need no price: which symbols, and when (relay.rails).
            rails = settings.get('rails')
            try:
                unified = unified_symbol(symbol)
            except ValueError:
                unified = symbol                           # not a form the lists can name: as sent
            for code, refusal in (('symbol_not_allowed', symbol_refusal(rails, unified)),
                                  ('outside_trading_hours', outside_trading_hours(rails, datetime.now(UTC)))):
                if refusal:
                    await _mark_signal_skipped(code)
                    logger.warning(f"[Entry] {symbol}: refused - {refusal}")
                    return {'success': False, 'reason': f'{code}: {refusal}'}

            # Position exists → pyramid / counter-reversal are not supported: skip
            positions = await self.position_tracker.get_all_positions()
            symbol_positions = [
                p for p in positions
                if p['symbol'] == symbol and p.get('deployment_id') == self.deployment_id
                and bool(p.get('dry_run')) == is_dry_run
            ]

            if symbol_positions:
                reason = 'position_exists'
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason}

            # The user's optional cooldown after closing this symbol
            if not signal.get('flip') and not await self._check_cooldown(symbol, is_dry_run):
                await _mark_signal_skipped('cooldown')
                logger.info(f"[Entry] {symbol}: refused - cooldown after the last close")
                return {'success': False, 'reason': 'cooldown'}

            # Expected entry price: the signal's, else the current market price
            expected_entry = float(signal.get('entry_price') or 0)
            if expected_entry <= 0:
                ticker = await self.bybit.get_ticker(symbol=symbol, category='linear')
                expected_entry = float(ticker.get('lastPrice', 0) or 0)
            if expected_entry <= 0:
                reason = 'no_price'
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason}
            direction = signal['direction']

            # 1. Risk Management Check
            risk_check = await self.risk_manager.check_trade_approval(symbol=symbol, signal=signal,
                                                                      is_dry_run=is_dry_run)

            if not risk_check['approved']:
                reason = f"risk_check_failed: {risk_check['reason']}"
                logger.warning(
                    f"[Entry] {symbol}: Risk check failed - {risk_check['reason']}"
                )
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason}

            # ── Pre-validate the stop BEFORE placing the market order ──
            stop_price = signal.get('stop_price')
            stop_price = float(stop_price) if stop_price else None
            if stop_price is None and signal.get('stop_pct'):
                # A stop given as a % from the entry: checked here from the expected entry price,
                # then set from the actual fill (step 4). On the price tick, rounded toward the
                # entry (never further than asked); without the venue's rules (read again, with
                # a refusal, below) it is aligned at step 4.
                pct = float(signal['stop_pct'])
                try:
                    specs = await self._instrument_specs(symbol)
                except Exception:
                    specs = None
                stop_price = round_stop_to_tick(expected_entry * (1 - pct / 100 if direction == 'long' else
                                                                  1 + pct / 100),
                                                specs['tick_size'] if specs else None, direction)
            require_stop = settings.get('require_stop', True)
            min_stop_pct = float(settings.get('min_stop_distance_pct') or 0)
            max_stop_pct = settings.get('max_stop_distance_pct')
            max_stop_pct = float(max_stop_pct) if max_stop_pct else None

            # Gate 1: a stop is mandatory unless the deployment explicitly disables it
            if stop_price is None and require_stop:
                await _mark_signal_skipped('stop_required')
                return {'success': False, 'reason': 'stop_required'}

            # Gate 1a: a long-only venue (spot) takes no short
            if direction == 'short' and not getattr(self.bybit, 'can_short', True):
                await _mark_signal_skipped('short_unsupported')
                return {'success': False, 'reason': 'short_unsupported'}

            # Gate 1b: a venue that cannot hold a stop order takes no entry that asks for one
            if stop_price is not None and not getattr(self.bybit, 'can_stop', True):
                await _mark_signal_skipped('stop_unsupported')
                return {'success': False, 'reason': 'stop_unsupported'}

            if stop_price is not None:
                # Gate 2: Refuse if the stop is on the wrong side of the expected entry
                if (direction == 'long' and stop_price >= expected_entry) or (
                        direction == 'short' and stop_price <= expected_entry):
                    await _mark_signal_skipped('stop_wrong_side')
                    return {'success': False, 'reason': 'stop_wrong_side'}

                # Gate 3: Refuse if the stop distance is too small to be a meaningful stop
                stop_distance_pct = abs(expected_entry - stop_price) / expected_entry * 100
                if stop_distance_pct < min_stop_pct:
                    await _mark_signal_skipped('stop_too_close')
                    return {'success': False, 'reason': 'stop_too_close'}

                # Gate 3b: Refuse if the stop is further than the user's maximum (when set)
                if max_stop_pct is not None and stop_distance_pct > max_stop_pct:
                    await _mark_signal_skipped('stop_too_far')
                    logger.warning(f"[Entry] {symbol}: refused - stop distance {stop_distance_pct:.3f}% > "
                                   f"{max_stop_pct}% maximum")
                    return {'success': False, 'reason': 'stop_too_far'}

            # Gate 3b2 (rail): a stop at or past the bankruptcy price for the leverage would never fire
            bankrupt = stop_past_bankruptcy(rails, direction, expected_entry, stop_price,
                                            signal.get('leverage') or settings.get('leverage'),
                                            settings.get('isolated_margin'))
            if bankrupt:
                await _mark_signal_skipped('stop_past_bankruptcy')
                logger.warning(f"[Entry] {symbol}: refused - {bankrupt}")
                return {'success': False, 'reason': f'stop_past_bankruptcy: {bankrupt}'}

            # Gate 3c: the trailing stop must be usable: the order's own (as a whole; empty = none),
            # else the deployment's (both unset = none)
            trailing = signal.get('trailing')
            if trailing is None:
                trailing = {'distance_pct': settings.get('trailing_distance_pct'),
                            'activate_pct': settings.get('trailing_activate_pct')}
            trailing_distance, trailing_activate = trailing.get('distance_pct'), trailing.get('activate_pct')
            trailing_issue = trailing_problem(trailing_distance, trailing_activate)
            if trailing_issue:
                await _mark_signal_skipped('trailing_invalid')
                logger.warning(f"[Entry] {symbol}: refused - {trailing_issue}")
                return {'success': False, 'reason': f'trailing_invalid: {trailing_issue}'}

            # Gate 3d: take-profits. An order's own (one price, or a ladder; an empty ladder = none)
            # replaces the deployment's (take_profit_pct or take_profit_ladder); one source may not
            # set both.
            order_ladder, order_single = signal.get('take_profits'), signal.get('take_profit_price')
            if order_ladder is not None or order_single is not None:
                ladder, use_default_tp = order_ladder or None, False
                tp_conflict = bool(order_ladder) and order_single is not None
            else:
                ladder = settings.get('take_profit_ladder')
                use_default_tp = not ladder
                tp_conflict = bool(ladder) and bool(settings.get('take_profit_pct'))
            tp_issue = ('set one take-profit: a single price or a ladder, not both' if tp_conflict
                        else ladder_problem(ladder) if ladder else None)
            if tp_issue is None and ladder:
                tp_issue = ladder_price_problem(direction, expected_entry, ladder)
            if tp_issue:
                await _mark_signal_skipped('take_profit_invalid')
                logger.warning(f"[Entry] {symbol}: refused - {tp_issue}")
                return {'success': False, 'reason': f'take_profit_invalid: {tp_issue}'}

            # Gate 3e: break-even, the same way: the order's own (empty = none), else the deployment's
            break_even = signal.get('break_even')
            if break_even is None:
                break_even = {'trigger_pct': settings.get('break_even_trigger_pct'),
                              'trigger_r': settings.get('break_even_trigger_r'),
                              'buffer_pct': settings.get('break_even_buffer_pct'),
                              'trigger_tp': settings.get('break_even_trigger_tp')}
            be_args = (break_even.get('trigger_pct'), break_even.get('trigger_r'), break_even.get('buffer_pct'))
            be_tp = break_even.get('trigger_tp')
            be_on = be_args[0] is not None or be_args[1] is not None or be_tp is not None
            be_issue = break_even_problem(*be_args, be_tp)
            if be_issue is None and be_tp is not None and len(ladder or []) < be_tp:
                be_issue = f'break-even after take-profit level {be_tp} needs a ladder with that level'
            if be_issue is None and be_on:
                try:
                    break_even_levels(direction, expected_entry, stop_price,
                                      {**dict(zip(('trigger_pct', 'trigger_r', 'buffer_pct'), be_args)),
                                       'trigger_tp': be_tp}, None)
                except ValueError as e:
                    be_issue = str(e)
            if be_issue:
                await _mark_signal_skipped('break_even_invalid')
                logger.warning(f"[Entry] {symbol}: refused - {be_issue}")
                return {'success': False, 'reason': f'break_even_invalid: {be_issue}'}

            # Gate 4: a take-profit the signal sends must be on the profit side of the entry
            signal_tp = signal.get('take_profit_price')
            if signal_tp is not None and (
                    (direction == 'long' and float(signal_tp) <= expected_entry)
                    or (direction == 'short' and float(signal_tp) >= expected_entry)):
                await _mark_signal_skipped('take_profit_wrong_side')
                logger.warning(f"[Entry] {symbol}: refused - take-profit {signal_tp} on the wrong side of "
                               f"entry {expected_entry} for {direction}")
                return {'success': False, 'reason': 'take_profit_wrong_side'}

            # 2. Calculate Position Size
            sized = compute_size(
                size=signal.get('size') or {},
                entry_price=expected_entry,
                equity=risk_check['max_size'],
                stop_price=stop_price,
            )

            if sized.quantity == 0:
                reason = f"position_size_rejected: {sized.reason}"
                logger.warning(f"[Entry] {symbol}: Position size rejected ({sized.reason}) - SKIPPED")
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason}

            # 2.5. Fit the quantity to the venue's own lot rules (quantity step, min/max quantity,
            # minimum order value). The rules come only from the venue: without them nothing is sent.
            # Rounding is always DOWN (never more than asked); the gap is reported with every result,
            # and refused when it exceeds the user's optional `max_size_shortfall_pct`.
            requested_qty = sized.quantity
            # Rails on the order's value (relay.rails): clip it to fit, or refuse.
            target_qty, rail_note, rail = requested_qty, None, None
            cap = None
            if rails and any(rails.get(k) is not None for k in
                             ('max_notional_usd', 'max_notional_pct', 'max_gross_leverage')):
                _, open_notional = await self.risk_manager._open_exposure(is_dry_run)
                cap = notional_cap(rails, risk_check['max_size'], open_notional)
            if cap is not None and requested_qty * expected_entry > cap[0] + 1e-9:
                cap_value, rail = cap
                detail = (f"{rail}: the order's value {requested_qty * expected_entry:.2f} is over the limit "
                          f"{cap_value:.2f}")
                if rails.get('on_limit') == 'reject' or cap_value <= 0:
                    await _mark_signal_skipped(f'rail_{rail}')
                    logger.warning(f"[Entry] {symbol}: refused - {detail}")
                    return {'success': False, 'reason': f'rail_{rail}: {detail}'}
                target_qty = cap_value / expected_entry
                rail_note = f'clipped to {rail} ({cap_value:.2f})'
                logger.info(f"[Entry] {symbol}: {detail} - clipped")
            try:
                specs = await self._instrument_specs(symbol)
            except Exception as e:
                logger.warning(f"[Entry] {symbol}: venue lot rules unavailable: {e}")
                specs = None
            if not specs or not specs.get('qty_step'):
                reason = 'instrument_rules_unavailable'
                logger.warning(f"[Entry] {symbol}: venue lot rules unknown - SKIPPED (no size is guessed)")
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason}
            qty_step, min_qty, max_qty = specs['qty_step'], specs['min_qty'], specs['max_qty']
            tick_size = specs['tick_size']

            quantity = round_qty_to_step(target_qty, qty_step)
            adjustment = 'lot_step' if quantity < target_qty else None
            if quantity > max_qty:
                quantity = round_qty_to_step(max_qty, qty_step)
                adjustment = 'venue_max_qty'
            # The venue's shortfall (what its lot rules took off what the rails left).
            shortfall_pct = (target_qty - quantity) / target_qty * 100 if target_qty > 0 else 0.0
            sizing = {
                'rails': rail_note,
                'requested_quantity': requested_qty,
                'quantity': quantity,
                'requested_notional': requested_qty * expected_entry,
                'notional': quantity * expected_entry,
                'shortfall_pct': round(shortfall_pct, 4),
                'adjustment': adjustment,
                'qty_step': qty_step,
            }

            async def _refuse_size(reason: str, detail: str) -> dict[str, Any]:
                logger.warning(f"[Entry] {symbol}: {detail} - SKIPPED")
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason, 'sizing': sizing}

            if rail_note and (quantity < min_qty or quantity <= 0):
                return await _refuse_size(
                    f'rail_{rail}',
                    f"{rail}: the rail leaves {target_qty:.8f}, below the venue minimum {min_qty}")
            if quantity < min_qty or quantity <= 0:
                return await _refuse_size(
                    'qty_below_minimum',
                    f"Quantity {requested_qty:.8f} is below the venue minimum {min_qty} (step {qty_step})")
            min_notional = specs.get('min_notional') or 0
            if min_notional and quantity * expected_entry < min_notional:
                return await _refuse_size(
                    'below_min_notional',
                    f"Order value {quantity * expected_entry:.2f} is below the venue minimum {min_notional}")
            max_shortfall = settings.get('max_size_shortfall_pct')
            if max_shortfall is not None and shortfall_pct > float(max_shortfall) + 1e-9:
                return await _refuse_size(
                    'size_shortfall_exceeded',
                    f"Venue lot rules shrink the order by {shortfall_pct:.2f}% "
                    f"(limit {float(max_shortfall):g}%): {quantity} instead of {requested_qty:.8f}")
            if adjustment:
                logger.info(
                    f"[Entry] {symbol}: size {requested_qty:.8f} -> {quantity} ({adjustment}, "
                    f"-{shortfall_pct:.2f}%, step={qty_step})"
                )

            # The new order counts toward the user's exposure limit (the pre-check above only saw
            # the positions already open).
            exposure = await self.risk_manager.check_new_exposure(quantity * expected_entry, is_dry_run)
            if not exposure['approved']:
                reason = f"risk_check_failed: {exposure['reason']}"
                logger.warning(f"[Entry] {symbol}: {reason}")
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason, 'sizing': sizing}

            # Last check before the first exchange write: an emergency stop that fired while this
            # entry was being prepared wins.
            if self.halted_reason:
                logger.warning(f"[Entry] {symbol}: refused before sending - engine halted ({self.halted_reason})")
                await _mark_signal_skipped('engine_halted')
                return {'success': False, 'reason': 'engine_halted'}

            # Take-profit orders an earlier position on this symbol left resting (reduce-only) would
            # reduce the new one: they go first, and a live entry waits while any may still rest.
            # (A paper entry makes no venue call at all: the stop-rules pass cleans up meanwhile.)
            leftover = 0
            if not is_dry_run:
                try:
                    _, leftover = await self._cancel_leftover_take_profits(symbol)
                except Exception as e:
                    logger.warning(f"[Entry] {symbol}: leftover take-profit cleanup failed ({e})")
                    leftover = None
            if leftover != 0:
                await _mark_signal_skipped('take_profit_leftover')
                logger.warning(f"[Entry] {symbol}: refused - take-profit orders of an earlier position may still "
                               f"rest on the venue (they would reduce this one); the stop-rules pass retries")
                return {'success': False, 'reason': 'take_profit_leftover'}

            # 2.6. Set leverage on Bybit before placing order (only when orders are real). No
            # shipped default: with no leverage configured the account's own leverage and margin
            # mode are left untouched and the position records none (the exchange reports it).
            configured_leverage = signal.get('leverage') or settings.get('leverage')
            leverage = float(configured_leverage) if configured_leverage else None
            if leverage is None:
                logger.info(f"[Entry] {symbol}: no leverage configured - using the account's current setting")
            elif not is_dry_run:
                # The user's explicit choices must hold, or the entry does not happen: trading at an
                # unknown leverage or margin mode can liquidate a position inside its stop.
                margin_mode = settings.get('isolated_margin')        # None = leave the account's mode
                try:
                    if margin_mode is not None:
                        resp = await self.bybit.switch_isolated_margin(
                            symbol=symbol, leverage=leverage, isolated=bool(margin_mode), category='linear')
                        if resp.get('retCode', -1) not in (0, 110026):   # 110026 = already in that mode
                            raise Exception(f"retCode={resp.get('retCode')} {resp.get('retMsg')}")
                    resp = await self.bybit.set_leverage(symbol=symbol, leverage=leverage, category='linear')
                    if resp.get('retCode', -1) not in (0, 110043):       # 110043 = already at that leverage
                        raise Exception(f"retCode={resp.get('retCode')} {resp.get('retMsg')}")
                    logger.info(f"[Entry] {symbol}: Leverage {leverage}x"
                                + (f", {'isolated' if margin_mode else 'cross'} margin" if margin_mode is not None else ""))
                except Exception as e:
                    reason = f"leverage_not_set: {e}"
                    logger.error(f"[Entry] {symbol}: refused - could not apply the configured leverage/margin "
                                 f"mode ({e}); not trading at an unknown leverage")
                    await _mark_signal_skipped(reason)
                    return {'success': False, 'reason': reason}

            # 3. Place Entry Order (tagged with the owning deployment for import attribution). A
            # live entry's stop is written down first: if the process dies before the fill is
            # recorded, the position import adopts it with this stop (see order_intents).
            entry_link = make_order_link(self.deployment_id)
            if not is_dry_run:
                await self._record_intent(entry_link, symbol, direction, stop_price)
            order_result = await self.order_executor.place_order(
                symbol=symbol,
                side=direction,
                quantity=quantity,
                order_type='Market',
                expected_price=expected_entry,
                order_link_id=entry_link,
                simulate=is_dry_run,   # decided once for the whole entry
            )

            # INTENT LOG: Mark that an exchange order was placed (for smart error recovery)
            if order_result.get('success'):
                order_id = order_result.get('order_id', '')
                if order_id and str(order_id).startswith('dry_run_'):
                    # DRY-RUN: No real order was placed, don't set order_placed_on_exchange
                    logger.info(f"[Entry] {symbol}: DRY-RUN order (no real exchange order)")
                else:
                    order_placed_on_exchange = True
                    logger.info(f"[Entry] {symbol}: Order placed: {order_id}")

            if not order_result['success']:
                raw_error = order_result.get('error', 'Unknown error')
                reason = f"order_failed: {raw_error}"
                logger.error(
                    f"[Entry] {symbol}: Order failed - {raw_error}"
                )
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason}

            # The real fill: price and quantity from the venue's record of the order (paper: the
            # simulated fill). A partial fill records what actually filled. When the venue could not
            # confirm in time, see _unconfirmed_entry_price; the source is recorded either way.
            fill_source = order_result.get('fill_source') or ('simulated' if is_dry_run else 'venue_order')
            filled = order_result.get('fill_quantity')
            if filled and float(filled) > 0 and abs(float(filled) - quantity) > 1e-12:
                logger.warning(f"[Entry] {symbol}: filled {filled} of {quantity} - recording what filled")
                quantity = float(filled)
            fill_price = order_result.get('fill_price')
            if not fill_price or fill_price <= 0:
                fill_price, fill_source = await self._unconfirmed_entry_price(
                    symbol, direction, quantity, expected_entry)
                order_result['fill_price'] = fill_price
            order_result['fill_source'] = fill_source
            sizing['filled_quantity'] = quantity
            sizing['entry_price_source'] = fill_source

            # 4. The stop as given, tick-aligned toward entry (risk can only shrink), then checked
            # against the ACTUAL fill. It is never moved: if the fill already passed it, the stop
            # would have fired, so the position is closed right after it is recorded (7b).
            stop_loss = {'stop_price': None}
            stop_breached = False
            stop_warnings: list[str] = []
            if signal.get('stop_pct') and not signal.get('stop_price'):
                # "% from the entry" is from the real entry: slippage must not eat into it.
                pct = float(signal['stop_pct'])
                stop_price = fill_price * (1 - pct / 100 if direction == 'long' else 1 + pct / 100)
            if stop_price is not None:
                aligned = round_stop_to_tick(stop_price, tick_size, direction)
                checked = self.stop_loss_manager.validate_stop(
                    side=direction,
                    entry_price=fill_price,
                    stop_price=aligned,
                    current_price=fill_price,
                    min_distance_pct=min_stop_pct or None,
                    max_distance_pct=max_stop_pct,
                )
                stop_loss = {'stop_price': aligned}
                stop_breached = not checked['valid']
                stop_warnings = checked['warnings']

            # 5. Take-profit: the signal's, else the user's optional take_profit_pct from the fill.
            # Tick-aligned; dropped (and reported) if the fill already passed it.
            take_profit_price = signal.get('take_profit_price')
            take_profit_pct = (settings.get('take_profit_pct') or 0) if use_default_tp else 0
            if take_profit_price is None and take_profit_pct > 0:
                entry = order_result['fill_price']
                if direction == 'long':
                    take_profit_price = entry * (1 + take_profit_pct / 100)
                else:
                    take_profit_price = entry * (1 - take_profit_pct / 100)
            take_profit_note = None
            if take_profit_price is not None:
                take_profit_price = round_price_to_tick(float(take_profit_price), tick_size)
                if (direction == 'long' and take_profit_price <= fill_price) or (
                        direction == 'short' and take_profit_price >= fill_price):
                    take_profit_note = f'take_profit_already_reached_at_fill ({take_profit_price})'
                    logger.warning(f"[Entry] {symbol}: {take_profit_note} - not set")
                    take_profit_price = None

            stop_result = None
            max_stop_attempts = 3 if stop_loss['stop_price'] is not None and not stop_breached else 0
            stop_retry_delay = 1.0
            if stop_breached:
                stop_result = {'success': False, 'error': 'stop_already_breached_at_fill'}
            elif max_stop_attempts == 0:
                stop_result = {'success': True, 'order_id': None}
                logger.warning(f"[Entry] {symbol}: no exchange stop (require_stop disabled for this deployment)")
                if take_profit_price is not None:
                    tp_result = await self.stop_loss_manager.place_take_profit(symbol, direction, take_profit_price,
                                                                               simulate=is_dry_run)
                    if not tp_result.get('success'):
                        take_profit_note = f"take_profit_not_set: {tp_result.get('error')}"
            for attempt in range(max_stop_attempts):
                if attempt == 0:
                    await asyncio.sleep(0.5)
                else:
                    await asyncio.sleep(stop_retry_delay * attempt)
                stop_result = await self.stop_loss_manager.place_stop_loss(
                    symbol=symbol,
                    side=direction,
                    quantity=quantity,
                    stop_price=stop_loss['stop_price'],
                    take_profit=take_profit_price,
                    simulate=is_dry_run,
                )
                if stop_result.get('success'):
                    logger.info(
                        f"[Entry] {symbol}: Stop-loss set @ ${stop_loss['stop_price']:.4f} "
                        f"(attempt {attempt + 1}/{max_stop_attempts})"
                    )
                    break
                else:
                    logger.warning(
                        f"[Entry] {symbol}: Stop-loss attempt {attempt + 1}/{max_stop_attempts} failed: "
                        f"{stop_result.get('error', 'Unknown error')}"
                    )

            if not stop_result or not stop_result.get('success'):
                stop_error = stop_result.get('error', 'Unknown error') if stop_result else 'No result'
                logger.error(
                    f"[Entry] {symbol}: Stop-loss placement FAILED after {max_stop_attempts} attempts: {stop_error}. "
                    f"Position will be created but REQUIRES MANUAL STOP-LOSS!"
                )
            # 6. Create Position Record
            position = await self._create_position_record(
                signal=signal,
                order_result=order_result,
                quantity=quantity,
                stop_loss=stop_loss,
                stop_result=stop_result,
                take_profit_price=take_profit_price,
                leverage=leverage,
                is_dry_run=is_dry_run,
            )

            # 7b. CRITICAL: If stop placement failed, immediately try once more, then close the
            # unprotected position as a safety measure. The result says exactly what happened.
            if not stop_result or not stop_result.get('success'):
                logger.warning(
                    f"[Entry] {symbol}: Stop placement failed during entry - triggering IMMEDIATE recovery..."
                )
                if stop_breached:
                    retry = {'success': False, 'error': 'stop_already_breached_at_fill'}
                else:
                    await asyncio.sleep(1.0)
                    retry = await self.stop_loss_manager.place_stop_loss(
                        symbol=symbol,
                        side=direction,
                        quantity=quantity,
                        stop_price=stop_loss['stop_price'],
                        take_profit=take_profit_price,
                        simulate=is_dry_run,
                    )
                if retry.get('success'):
                    logger.info(f"[Entry] {symbol}: Immediate stop-loss recovery completed")
                else:
                    logger.error(
                        f"[Entry] {symbol}: Immediate stop-loss recovery FAILED: {retry.get('error')}. "
                        f"Closing unprotected position as safety measure."
                    )
                    close = await self.execute_exit_fast(position=position, reason="emergency_no_stop_loss")
                    confirmed = False
                    if close.get('in_progress'):
                        close = {'success': False, 'unconfirmed': True,
                                 'error': 'another close is already handling this position'}
                    if close.get('success') and not close.get('in_progress'):
                        outcome = {}
                        try:
                            done = await self.finalize_closed_positions([close])
                            outcome = done[0] if done else {}
                        except Exception as e:
                            outcome = {'error': str(e)}
                        # Closed only if the close was confirmed and recorded; anything else
                        # (nothing / part filled, unconfirmed, no price, an error) is not closed.
                        confirmed = 'pnl_source' in outcome or bool(close.get('already_closed'))
                        if not confirmed:
                            open_for_sure = outcome.get('nothing_filled') or outcome.get('partial')
                            close = {'success': False, 'unconfirmed': not open_for_sure,
                                     'error': f"safety close not confirmed: {outcome or 'no outcome'}"}
                    if confirmed:
                        logger.warning(f"[Entry] {symbol}: Closed unprotected position after SL failure")
                        # A fill past the stop = the stop would already have fired: same outcome.
                        reason = ('stop_breached_at_fill_position_closed' if stop_breached
                                  else 'stop_failed_position_closed')
                    elif close.get('unconfirmed'):
                        logger.critical(
                            f"[Entry] {symbol}: the safety close was sent but is NOT CONFIRMED "
                            f"({close.get('error')}) - the position may still be open WITHOUT A STOP; the "
                            f"position sync settles it against the venue and the stop audit keeps retrying."
                        )
                        reason = 'stop_failed_close_unconfirmed'
                    else:
                        logger.critical(
                            f"[Entry] {symbol}: CRITICAL - Failed to close unprotected position: "
                            f"{close.get('error')}. The position is OPEN WITHOUT A STOP - MANUAL "
                            f"INTERVENTION REQUIRED (the stop audit keeps retrying)."
                        )
                        reason = 'stop_failed_position_open_unprotected'
                    await self._annotate_signal(signal.get('signal_id'), reason)
                    danger = reason in ('stop_failed_close_unconfirmed', 'stop_failed_position_open_unprotected')
                    await self._event(
                        'position.stop_failed', 'critical' if danger else 'warning',
                        f"{symbol} {direction}: the stop could not be set - "
                        + ('the position may be OPEN WITHOUT A STOP: check it now' if danger
                           else 'the position was closed again'),
                        {'symbol': symbol, 'side': direction, 'outcome': reason, 'paper': is_dry_run,
                         'stop_error': retry.get('error'), 'close_error': close.get('error')}, position['id'])
                    return {'success': False, 'reason': reason, 'position_id': position['id'],
                            'fill_price': order_result['fill_price'], 'quantity': quantity,
                            'dry_run': is_dry_run, 'sizing': sizing,
                            'stop_error': retry.get('error'), 'close_error': close.get('error')}

            # The trailing stop goes on after the fixed stop: the position is already protected, so a
            # trailing stop the venue refuses is reported, not a reason to close.
            trailing_result = None
            if trailing_distance:
                trailing_result = await self.set_trailing(position, trailing_distance, trailing_activate,
                                                          reason='entry')
                if not trailing_result.get('success'):
                    logger.warning(f"[Entry] {symbol}: trailing stop not set: {trailing_result.get('error')}")

            # The ladder before break-even: a break-even after take-profit level n needs the levels.
            ladder_result = None
            if ladder:
                ladder_result = await self.set_take_profits(position, ladder, reason='entry')
                if not ladder_result.get('success'):
                    logger.warning(f"[Entry] {symbol}: take-profit ladder not set: {ladder_result.get('error')}")

            break_even_result = None
            if be_on:
                break_even_result = await self.set_break_even(position, *be_args, reason='entry',
                                                              trigger_tp=be_tp)
                if not break_even_result.get('success'):
                    logger.warning(f"[Entry] {symbol}: break-even not armed: {break_even_result.get('error')}")

            stop_str = f"${stop_loss['stop_price']:,.2f}" if stop_loss['stop_price'] is not None else "none"
            logger.info(
                f"[Entry] {symbol}: ✓ Entered {direction} @ "
                f"${order_result['fill_price']:,.2f} "
                f"(Stop: {stop_str})"
            )

            if stop_result and stop_result.get('take_profit_error'):
                take_profit_note = f"take_profit_not_set: {stop_result['take_profit_error']}"
            return {'success': True, 'fill_price': order_result['fill_price'],
                    'position_id': position['id'], 'quantity': quantity, 'dry_run': is_dry_run,
                    'sizing': sizing, 'stop_price': stop_loss['stop_price'],
                    'stop_warnings': stop_warnings, 'take_profit_price': take_profit_price,
                    'take_profit_note': take_profit_note,
                    'trailing': trailing_result.get('trailing') if trailing_result else None,
                    'trailing_note': (f"trailing_not_set: {trailing_result.get('error')}"
                                      if trailing_result and not trailing_result.get('success') else None),
                    'take_profits': ladder_result.get('take_profits') if ladder_result else None,
                    'take_profits_note': (f"take_profits_not_set: {ladder_result.get('error')}"
                                          if ladder_result and not ladder_result.get('success') else
                                          '; '.join(ladder_result['warnings'])
                                          if ladder_result and ladder_result.get('warnings') else None),
                    'break_even': break_even_result.get('break_even') if break_even_result else None,
                    'break_even_note': (f"break_even_not_set: {break_even_result.get('error')}"
                                        if break_even_result and not break_even_result.get('success') else None)}

        except Exception as e:
            error_msg = str(e)
            logger.opt(exception=True).error(f"[Entry] {symbol}: Execution failed - {error_msg}")

            # INTENT LOG PATTERN: Smart exception handling
            if order_placed_on_exchange and order_result and order_result.get('success'):
                # Exchange order succeeded but something else failed (likely DB write)
                # DON'T mark signal as skipped - leave it for recovery
                logger.warning(
                    f"[Entry] {symbol}: DB write failed but the exchange order succeeded! "
                    f"Order ID: {order_result.get('order_id')}. "
                    f"Signal {signal_id} left for recovery. "
                    f"Triggering immediate import..."
                )

                # Trigger immediate recovery attempt (the order-link tag attributes it to us)
                try:
                    import_result = await self.position_tracker.import_exchange_positions()
                    if import_result.get('imported_count', 0) > 0:
                        logger.info(
                            f"[Entry] {symbol}: Recovery import successful! "
                            f"Imported {import_result['imported_count']} position(s)"
                        )
                        # Mark signal as executed since import succeeded
                        if signal_id and self.signal_log is not None:
                            await self.signal_log.mark_executed(signal_id)
                    else:
                        logger.warning(
                            f"[Entry] {symbol}: Immediate import found no new positions. "
                            f"The sync loop will reconcile later."
                        )
                except Exception as import_err:
                    logger.error(
                        f"[Entry] {symbol}: Immediate import failed: {import_err}. "
                        f"The sync loop will reconcile."
                    )

                # Return failure but DON'T mark signal as skipped
                return {'success': False, 'reason': f"db_write_failed_recovery_pending: {error_msg}"}
            else:
                reason = f"execution_error: {error_msg}"
                await _mark_signal_skipped(reason)
                return {'success': False, 'reason': reason}

    # ---- closing ------------------------------------------------------------------------
    # Every close works from the row's CURRENT database state, under the position's lock (shared
    # with partial closes). While the relay closes a position its id is in `self._closing`: the
    # live-feed handler and the position sync leave it alone, so the relay's own record (which
    # knows the real fill) is the only one written; every write is still status-guarded.

    def _position_lock(self, position_id: int) -> asyncio.Lock:
        return self._reduce_locks.setdefault(position_id, asyncio.Lock())

    def _close_owned(self, position_id: int) -> bool:
        """True while the relay is closing this position. Ownership older than CLOSE_OWNERSHIP_S
        is ignored (a backstop: a close interrupted in a way that skipped its release must not
        hide the position from the sync and the live feed for good)."""
        since = self._closing.get(position_id)
        if since is None:
            return False
        if _time.monotonic() - since > CLOSE_OWNERSHIP_S:
            logger.error(f"[Close] position {position_id}: close ownership older than {CLOSE_OWNERSHIP_S}s "
                         f"- released; the position sync takes over")
            self._closing.pop(position_id, None)
            return False
        return True

    def _own_close(self, position_id: int) -> bool:
        """Take ownership of closing a position; False if another close already owns it."""
        if self._close_owned(position_id):
            return False
        self._closing[position_id] = _time.monotonic()
        return True

    def _release_close(self, position_id: int) -> None:
        self._closing.pop(position_id, None)

    async def _fresh_row(self, position_id: int) -> dict[str, Any] | None:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, symbol, side, entry_price, size, opened_at, dry_run, status, entry_meta, "
                "deployment_id, stop_price, initial_stop_price, management FROM positions WHERE id = $1", position_id)
        return dict(row) if row else None

    async def _record_partial(self, conn, row: dict[str, Any], qty: float, exit_price: float | None,
                              order_id: str | None, reason: str, paper: bool) -> float | None:
        """Record `qty` of the position as closed (trade_type 'partial_exit') and reduce its size.
        Returns the partial PnL, or None when it cannot be computed from real fills."""
        return (await self._record_partial_tx(conn, row, qty, exit_price, order_id, reason, paper))[1]

    async def _record_partial_tx(self, conn, row: dict[str, Any], qty: float, exit_price: float | None,
                                 order_id: str | None, reason: str, paper: bool,
                                 management: dict | None = None) -> tuple[bool, float | None]:
        """_record_partial, returning (recorded, pnl). Nothing is recorded for a position no longer
        open or closing (a close recorded meanwhile already counts this quantity: its exit is the
        whole size it found). `management`, when given, is written in the same transaction, so a
        record and the state that says it was made can never part."""
        from relay.execution.close_record import _entry_confirmed
        entry = float(row['entry_price'])
        pnl = None
        if exit_price and _entry_confirmed(row):
            pnl = ((exit_price - entry) if str(row['side']).upper() == 'LONG' else (entry - exit_price)) * qty
        now = datetime.now(UTC)
        async with conn.transaction():
            updated = await conn.fetchval(
                "UPDATE positions SET size = size - $1, updated_at = $2"
                + (", management = $4" if management is not None else "")
                + " WHERE id = $3 AND status IN ('OPEN', 'CLOSING') RETURNING id",
                qty, now, row['id'], *([json.dumps(management)] if management is not None else []))
            if updated is None:
                logger.warning(f"[Exit] {row['symbol']} #{row['id']}: a partial close of {qty} was not recorded "
                               f"- the position is no longer open (its close record covers it)")
                return False, None
            await conn.execute(
                "INSERT INTO trades (position_id, symbol, trade_type, side, qty, price, time, "
                "order_id, exit_reason, metadata, created_at, updated_at) "
                "VALUES ($1, $2, 'partial_exit', $3, $4, $5, $6, $7, $8, $9, $6, $6)",
                row['id'], row['symbol'], 'SELL' if str(row['side']).upper() == 'LONG' else 'BUY',
                qty, exit_price, now, None if paper else order_id, reason, json.dumps({'pnl': pnl}),
            )
        return True, pnl

    async def execute_exit(
        self,
        position: dict[str, Any],
        reason: str = 'manual',
        client=None,
    ) -> str:
        """
        Close a position fully (reduce-only market order) and record the result.

        Raises if the close failed or only part of the position closed (the filled part is then
        recorded as a partial close and the rest stays OPEN). Returns 'closed', 'not_open' (nothing
        to do) or 'in_progress' (another close owns it and records the result).

        Args:
            position: Position dict (its id identifies the row; size and mode are re-read)
            reason: Exit reason (e.g., 'api', 'emergency_stop')
            client: optional per-account client/adapter. Defaults to self.bybit.
        """
        async with self._position_lock(position['id']):
            return await self._execute_exit_locked(position, reason, client)

    async def _venue_open_size(self, symbol: str, side: str, client) -> float | None:
        """The venue's open size for this symbol and side (0 if none), or None if unreadable."""
        import inspect
        want = 'Buy' if str(side).upper() == 'LONG' else 'Sell'
        kwargs = {'category': 'linear', 'symbol': symbol}
        try:
            # A safety read: it must work while the breaker is open (that is when CLOSING rows occur).
            if "bypass_breaker" in inspect.signature(client.get_positions).parameters:
                kwargs['bypass_breaker'] = True
        except (TypeError, ValueError):
            pass
        try:
            rows = await client.get_positions(**kwargs)
        except Exception as e:
            logger.warning(f"[Exit] {symbol}: venue position read failed: {e}")
            return None
        return sum(float(p.get('size') or 0) for p in rows if p.get('side') == want)

    async def _retire_ladder_for_close(self, row: dict[str, Any]) -> None:
        """Before a closing position's venue size is measured: cancel its take-profit levels and mark
        every one no longer open, so a fill is never counted both in that measurement and by the
        fill check. (One the venue will not cancel and does not show as done stays open: the
        cleanup of closed positions retries it.)"""
        management = _json_obj(row.get('management'))
        levels = (management.get('take_profits') or {}).get('levels') or []
        if not any(lv.get('status') == 'open' for lv in levels):
            return
        await self._cancel_ladder_orders(row['symbol'], levels, False, 'position closing', settle_gone=True)
        async with self.db.acquire() as conn:
            await conn.execute("UPDATE positions SET management = $1 WHERE id = $2 AND status IN ('OPEN', 'CLOSING')",
                               json.dumps(management), row['id'])

    async def _execute_exit_locked(self, position: dict[str, Any], reason: str, client=None) -> str:
        """Returns 'closed', 'not_open' (nothing to do), or 'in_progress' (another close owns it and
        records the result). Raises if the close failed or only partly filled."""
        position_id = position['id']
        row = await self._fresh_row(position_id)
        if row is None or row['status'] not in ('OPEN', 'CLOSING'):
            logger.info(f"[Exit] position {position_id} is no longer open - nothing to close")
            return 'not_open'
        symbol = row['symbol']
        size = float(row['size'])
        _c = client or self.bybit
        if not self._own_close(position_id):
            # A fast close already sent this position's close; it records the result. Sending a
            # second reduce-only order could reduce another deployment's netted position.
            logger.info(f"[Exit] {symbol}: a close is already in progress - not sending another")
            return 'in_progress'
        logger.info(f"[Exit] {symbol}: Closing {row['side']} position (reason: {reason})")
        try:
            if row['status'] == 'CLOSING' and not row['dry_run']:
                # An earlier close was sent but never confirmed. Ask the venue what is still open
                # before sending anything: never a second full-size close on top of a filled one.
                # Take-profit orders go first: what they filled is then part of the difference
                # recorded below, and never recorded again as a take-profit fill.
                await self._retire_ladder_for_close(row)
                venue_size = await self._venue_open_size(symbol, row['side'], _c)
                if venue_size is None:
                    raise Exception(f"Failed to close position {symbol}: an earlier close is unconfirmed and "
                                    f"the venue position cannot be read")
                if venue_size <= 1e-12:
                    closed_at = datetime.now(UTC)
                    priced = await price_close(self.db, _c, row, closed_at)
                    async with self.db.acquire() as conn:
                        wrote = await write_close(conn, row, priced, closed_at, 'closed_on_venue')
                    if wrote:
                        self._last_exit_time[(symbol, False)] = datetime.now(UTC)
                    logger.info(f"[Exit] {symbol}: the earlier close had filled - recorded")
                    return 'closed'
                if venue_size > size + 1e-12:
                    # More is open than this position: something else on the same symbol and side
                    # (opened outside the relay). Which part is ours cannot be told - not guessed.
                    raise Exception(f"Failed to close position {symbol}: the venue holds {venue_size}, more "
                                    f"than this position's {size}, after an unconfirmed close - check manually")
                if venue_size < size - 1e-12:
                    # Part of it closed earlier at a price nobody confirmed: record exactly that (a
                    # partial with unknown PnL, priced later from the venue), then close the rest.
                    async with self.db.acquire() as conn:
                        await self._record_partial(conn, row, size - venue_size, None, None,
                                                   'closed_on_venue', False)
                    row = await self._fresh_row(position_id)
                    size = float(row['size'])
            close_result = await self.order_executor.close_position(
                symbol=symbol,
                side=row['side'],
                quantity=size,
                client=_c,
                simulate=_position_mode(row),
            )
            if not close_result['success']:
                error_msg = close_result.get('error', 'Unknown error')
                logger.error(f"[Exit] {symbol}: Close failed - {error_msg}")
                # Raise so callers know the close failed (close-all must not report success)
                raise Exception(f"Failed to close position {symbol}: {error_msg}")

            is_paper = str(close_result.get('order_id') or '').startswith('dry_run_')

            filled = close_result.get('fill_quantity')
            if filled is not None and 0 < float(filled) < size - 1e-12:
                async with self.db.acquire() as conn:
                    await self._record_partial(conn, row, float(filled), close_result.get('fill_price'),
                                               close_result.get('order_id'), reason, is_paper)
                logger.critical(f"[Exit] {symbol}: only {filled} of {size} closed - "
                                f"{size - float(filled)} STILL OPEN")
                raise Exception(f"Failed to close position {symbol}: only {filled} of {size} closed; "
                                f"{size - float(filled)} still open")
            # Fully closed: only now does a separate stop order go (the rest would need it).
            await self._cancel_separate_stop(position, symbol, _c, '[Exit]')

            # Record the close (relay.execution.close_record): the venue's verified result when its
            # records for the hold window are complete, else the real fills (local_calc), else pending.
            closed_at = datetime.now(UTC)
            if not is_paper:
                await asyncio.sleep(1.5)   # give the venue a moment to publish the close record
            priced = await price_close(
                self.db, _c, row, closed_at, exit_fill=close_result.get('fill_price'),
                paper=is_paper, close_order_id=None if is_paper else close_result.get('order_id'))
            async with self.db.acquire() as conn:
                await write_close(conn, row, priced, closed_at, reason)
            self._last_exit_time[(symbol, is_paper)] = datetime.now(UTC)
            logger.info(
                f"[Exit] {symbol}: ✓ Closed @ {priced['exit_price'] or 'unconfirmed price'} "
                f"(PnL: {priced['pnl'] if priced['pnl'] is not None else 'pending'}, "
                f"source {priced['pnl_source']})"
            )
            return 'closed'
        except Exception as e:
            logger.opt(exception=True).error(f"[Exit] {symbol}: Exit failed - {e}")
            raise
        finally:
            self._release_close(position_id)

    async def _cancel_separate_stop(self, position: dict[str, Any], symbol: str, client, tag: str) -> None:
        """Cancel a separate stop order if there is one (a position-level trading stop goes away
        with the position). Awaited, so a failure is seen."""
        if not (position.get('stop_loss_order_id') and self.stop_loss_manager):
            return
        try:
            if not await self.stop_loss_manager.cancel_stop_loss(
                    position['stop_loss_order_id'], symbol, client=client):
                logger.warning(f"{tag} {symbol}: stop order {position['stop_loss_order_id']} "
                               f"could not be cancelled - check for a leftover order")
        except Exception as e:
            logger.warning(f"{tag} {symbol}: Failed to cancel stop-loss: {e}")

    async def execute_exit_fast(
        self,
        position: dict[str, Any],
        reason: str = 'close_all',
        client=None,
    ) -> dict[str, Any]:
        """
        Send a position's reduce-only close without waiting for the fill (bulk closes). The row
        becomes CLOSING and stays owned by the relay until finalize_closed_positions records the
        fill and the result. Returns {'success', 'position_id', 'symbol', 'close_order_id',
        'reason', 'error', 'already_closed'?}.
        """
        position_id = position['id']
        async with self._position_lock(position_id):
            row = await self._fresh_row(position_id)
            if row is None or row['status'] == 'CLOSED':
                return {'success': True, 'already_closed': True, 'position_id': position_id,
                        'symbol': position.get('symbol'), 'error': None}
            if row['status'] == 'CLOSING' and not self._close_owned(position_id):
                # An earlier close was never confirmed: settle it against the venue (closes only
                # what is still open there) instead of calling it closed.
                try:
                    status = await self._execute_exit_locked(position, reason, client)
                except Exception as e:
                    return {'success': False, 'position_id': position_id, 'symbol': row['symbol'],
                            'error': str(e)}
                return {'success': True, 'already_closed': status != 'in_progress',
                        'in_progress': status == 'in_progress', 'position_id': position_id,
                        'symbol': row['symbol'], 'error': None}
            symbol = row['symbol']
            _c = client or self.bybit
            if not self._own_close(position_id):
                return {'success': True, 'in_progress': True, 'position_id': position_id,
                        'symbol': symbol, 'error': None}
            logger.info(f"[FastExit] {symbol}: Closing {row['side']} position (reason: {reason})")
            try:
                close_result = await self.order_executor.close_position_fast(
                    symbol=symbol,
                    side=row['side'],
                    quantity=float(row['size']),
                    client=_c,
                    simulate=_position_mode(row),
                )
            except BaseException:
                self._release_close(position_id)       # not sent (or unknown): not ours to finish
                raise
            if not close_result['success']:
                self._release_close(position_id)
                error = f"Failed to close position {symbol}: {close_result.get('error', 'Unknown error')}"
                logger.error(f"[FastExit] {error}")
                return {'success': False, 'position_id': position_id, 'symbol': symbol, 'error': error}
            # The close order is SENT: from here the result is a success the finalize step records,
            # even if this bookkeeping fails (reporting it as failed would get it sent again).
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        "UPDATE positions SET status = 'CLOSING', updated_at = $1 WHERE id = $2 AND status = 'OPEN'",
                        datetime.now(UTC), position_id)
            except Exception as e:
                logger.error(f"[FastExit] {symbol}: close sent, but marking it CLOSING failed ({e}) - "
                             f"finalize records it")
            self._last_exit_time[(symbol, bool(row['dry_run']))] = datetime.now(UTC)
            logger.info(f"[FastExit] {symbol}: ✓ Close order placed (id: {close_result.get('order_id')})")
            return {
                'success': True,
                'position_id': position_id,
                'symbol': symbol,
                'close_order_id': close_result.get('order_id'),
                'stop_loss_order_id': position.get('stop_loss_order_id'),
                'reason': reason,
                'error': None,
            }

    async def finalize_closed_positions(
        self,
        closed_positions: list[dict[str, Any]],
        client=None,
    ) -> list[dict[str, Any]]:
        """
        Record the result of closes placed with execute_exit_fast (rows CLOSING and owned by the
        relay), in one batch after all closes were sent. Reads each close order's real outcome:
          - filled fully: priced like any close (relay.execution.close_record)
          - filled partly: the filled part is recorded, the rest goes back to OPEN (reported)
          - filled nothing: back to OPEN (reported)
          - not confirmed in time: left CLOSING for the position sync to settle against the venue
          - paper with no price to simulate the fill: left CLOSING for the sweep to retry
        Ownership is released for every position, also if this is interrupted.
        Returns [{'position_id', 'symbol', ...}] with exit_price/pnl/pnl_source, or a flag
        ('partial', 'nothing_filled', 'unconfirmed', 'no_price').
        """
        todo = [c for c in (closed_positions or [])
                if c.get('success') and not c.get('already_closed') and not c.get('in_progress')]
        if not todo:
            return []
        _c = client or self.bybit
        finalized = []
        try:
            if any(not str(c.get('close_order_id') or '').startswith('dry_run_') for c in todo):
                await asyncio.sleep(1.5)   # give the venue a moment to publish the close records
            for close in todo:
                position_id = close['position_id']
                if position_id in self._closing:
                    self._closing[position_id] = _time.monotonic()   # still ours: refresh the claim
                try:
                    out = await self._finalize_one(close, _c)
                    if out is not None:
                        finalized.append(out)
                except Exception as e:
                    # Left CLOSING; with ownership released the position sync (live) or the
                    # stale-close sweep (paper) settles it. Reported, so the caller does not count it.
                    logger.error(f"[Finalize] {close.get('symbol')} #{position_id}: {e}")
                    finalized.append({'position_id': position_id, 'symbol': close.get('symbol'),
                                      'error': str(e)})
                finally:
                    self._release_close(position_id)
        finally:
            for close in todo:
                self._release_close(close['position_id'])
        return finalized

    async def _finalize_one(self, close: dict[str, Any], _c) -> dict[str, Any] | None:
        position_id = close['position_id']
        row = await self._fresh_row(position_id)
        if row is None or row['status'] == 'CLOSED':
            return None
        symbol, size, paper = row['symbol'], float(row['size']), bool(row['dry_run'])
        order_id = close.get('close_order_id')
        reason = close.get('reason') or 'close_all'
        base = {'position_id': position_id, 'symbol': symbol}
        if paper:
            exit_fill = await self.order_executor._get_current_price(symbol, client=_c) or None
            if exit_fill is None:
                logger.warning(f"[Finalize] {symbol} #{position_id}: no price to simulate the paper close - "
                               f"left CLOSING, retried by the sweep")
                return {**base, 'no_price': True}
            filled = size
        else:
            fill = await self.order_executor._fetch_fill(symbol, order_id=order_id, client=_c)
            if fill is None:
                logger.warning(f"[Finalize] {symbol} #{position_id}: close fill not confirmed - left CLOSING; "
                               f"the position sync settles it against the venue")
                return {**base, 'unconfirmed': True}
            exit_fill = fill.get('avg_price')
            filled = float(fill.get('qty') or 0) or (size if fill.get('status') == 'Filled' else 0.0)
        if filled <= 0:
            async with self.db.acquire() as conn:
                await conn.execute("UPDATE positions SET status = 'OPEN' WHERE id = $1 AND status = 'CLOSING'",
                                   position_id)
            logger.critical(f"[Finalize] {symbol} #{position_id}: the close filled NOTHING - position STILL OPEN")
            return {**base, 'nothing_filled': True, 'remaining_qty': size}
        if filled < size - 1e-12:
            async with self.db.acquire() as conn:
                await self._record_partial(conn, row, filled, exit_fill, order_id, reason, paper)
                await conn.execute("UPDATE positions SET status = 'OPEN' WHERE id = $1 AND status = 'CLOSING'",
                                   position_id)
            logger.critical(f"[Finalize] {symbol} #{position_id}: only {filled} of {size} closed - "
                            f"{size - filled} STILL OPEN")
            return {**base, 'partial': True, 'closed_qty': filled, 'remaining_qty': size - filled}
        # Fully closed: only now does a separate stop order go.
        await self._cancel_separate_stop(close, symbol, _c, '[Finalize]')
        closed_at = datetime.now(UTC)
        priced = await price_close(self.db, _c, row, closed_at, exit_fill=exit_fill, paper=paper,
                                   close_order_id=None if paper else order_id)
        async with self.db.acquire() as conn:
            await write_close(conn, row, priced, closed_at, reason)
        logger.info(f"[Finalize] {symbol} #{position_id}: exit {priced['exit_price']} "
                    f"PnL {priced['pnl']} ({priced['pnl_source']})")
        return {**base, **{k: priced[k] for k in ('exit_price', 'pnl', 'pnl_percent', 'pnl_source')}}

    async def execute_increase(self, position: dict[str, Any], add_quantity: float,
                               reason: str = 'increase') -> dict[str, Any]:
        """
        Add `add_quantity` (base units, before the venue's lot rounding) to an open position: one
        market order in the position's direction, then the row's size grows and its entry price
        becomes the size-weighted average, recorded with the add (trade_type 'entry') in one
        transaction. The position's venue stop is a whole-position stop (Full mode), so it covers
        the added size without being re-sent; a paper position's add is simulated.

        The checks a new entry would get apply: the deployment's enabled switch and halt, the
        symbol and trading-hours rails, max_notional (the position's value after the add) and
        max_gross_leverage (clip or reject), the exposure limit and the daily loss limit. A live
        position whose venue position other rows share is refused (the add would be theirs too).
        An add whose fill the venue cannot confirm is recorded at the expected price and marks the
        position's entry 'expected_unconfirmed' (its PnL then waits for the venue's records).

        Returns {'success': True, 'added_quantity', 'fill_price', 'size', 'entry_price', 'sizing'}
        or {'success': False, 'reason', 'error'}.
        """
        position_id = position['id']

        def refuse(reason: str, error: str, **extra) -> dict[str, Any]:
            logger.warning(f"[Increase] position {position_id}: {error}")
            return {'success': False, 'reason': reason, 'error': error, **extra}

        settings = await self._get_execution_settings()
        if settings.get('enabled') is False:
            return refuse('deployment_disabled', 'the deployment is disabled (enabled: false)')
        if self.halted_reason:
            return refuse('engine_halted', f'the engine is halted ({self.halted_reason})')
        if not add_quantity or add_quantity <= 0:
            return refuse('invalid', f'nothing to add ({add_quantity})')
        rails = settings.get('rails')
        async with self._position_lock(position_id):
            ctx, refusal = await self._stop_change_context(position_id)
            if refusal:
                return refuse(refusal['code'], refusal['error'])
            row, side, mark, paper = ctx['row'], ctx['side'], ctx['mark'], ctx['paper']
            symbol, size = row['symbol'], float(row['size'])
            # Rules set for the old size and entry (a ladder sized on it, a break-even or trailing
            # stop priced from it) would be wrong for the larger position: remove them first.
            management = _json_obj(row.get('management'))
            managed = [name for name, on in (
                ('take-profit ladder', any(lv.get('status') == 'open'
                                           for lv in (management.get('take_profits') or {}).get('levels') or [])),
                ('break-even', bool(management.get('break_even'))),
                ('trailing stop', bool(management.get('trailing')))) if on]
            if managed:
                return refuse('managed_position', f"{symbol}: the position has a {', '.join(managed)} set for its "
                                                  f"current size and entry - remove it before adding")
            if not paper:
                # The venue must hold exactly this row's size: an add sent onto a position the venue
                # has just closed (its stop) or changed would be recorded on a wrong picture.
                venue_size = await self._venue_open_size(symbol, row['side'], self.bybit)
                if venue_size is None or abs(venue_size - size) > max(1e-12, size * 1e-6):
                    return refuse('out_of_sync', f"{symbol}: the venue holds {venue_size} {side}, the record {size} "
                                                 f"- nothing added")
            try:
                unified = unified_symbol(symbol)
            except ValueError:
                unified = symbol
            for code, why in (('symbol_not_allowed', symbol_refusal(rails, unified)),
                              ('outside_trading_hours', outside_trading_hours(rails, datetime.now(UTC)))):
                if why:
                    return refuse(code, why)

            # Rails on value: the position after the add (max_notional_*), the book (gross leverage).
            target_qty, rail_note = float(add_quantity), None
            if rails and any(rails.get(k) is not None for k in
                             ('max_notional_usd', 'max_notional_pct', 'max_gross_leverage')):
                equity, _ = await self.bybit.get_balance_safe(coin="USDT")
                _, open_notional = await self.risk_manager._open_exposure(paper)
                caps = [c for c in (
                    notional_cap({k: rails.get(k) for k in ('max_notional_usd', 'max_notional_pct')},
                                 equity, 0.0),
                    notional_cap({'max_gross_leverage': rails.get('max_gross_leverage')}, equity, open_notional),
                ) if c is not None]
                for cap_value, rail in caps:
                    room = cap_value - (size * mark if rail != 'max_gross_leverage' else 0.0)
                    if target_qty * mark > room + 1e-9:
                        detail = f"{rail}: adding {target_qty * mark:.2f} would pass the limit ({room:.2f} left)"
                        if rails.get('on_limit') == 'reject' or room <= 0:
                            return refuse(f'rail_{rail}', detail)
                        target_qty, rail_note = room / mark, f'clipped to {rail}'
                        logger.info(f"[Increase] {symbol} #{position_id}: {detail} - clipped")

            specs = await self._instrument_specs(symbol)
            if not specs or not specs.get('qty_step'):
                return refuse('instrument_rules_unavailable', f'{symbol}: venue lot rules unknown')
            quantity = round_qty_to_step(target_qty, specs['qty_step'])
            quantity = min(quantity, round_qty_to_step(specs['max_qty'], specs['qty_step']))
            sizing = {'rails': rail_note, 'requested_quantity': float(add_quantity), 'quantity': quantity}
            if quantity <= 0 or quantity < specs['min_qty']:
                return refuse('qty_below_minimum', f'{symbol}: adding {target_qty:.8f} is below the venue '
                                                   f'minimum {specs["min_qty"]}', sizing=sizing)
            if (specs.get('min_notional') or 0) and quantity * mark < specs['min_notional']:
                return refuse('below_min_notional', f'{symbol}: an add of {quantity * mark:.2f} is below the '
                                                    f'venue minimum {specs["min_notional"]}', sizing=sizing)
            balance = await self.risk_manager._check_account_balance()
            if not balance['approved']:
                return refuse('risk_check_failed', balance['reason'])
            for check in (await self.risk_manager._check_daily_loss_limit(balance['max_size'], paper),
                          await self.risk_manager.check_new_exposure(quantity * mark, paper)):
                if not check['approved']:
                    return refuse('risk_check_failed', check['reason'], sizing=sizing)

            order = await self.order_executor.place_order(
                symbol=symbol, side=side, quantity=quantity, order_type='Market', expected_price=mark,
                order_link_id=make_order_link(self.deployment_id), simulate=_position_mode(row))
            if not order.get('success'):
                return refuse('order_failed', humanize_order_error(str(order.get('error'))), sizing=sizing)
            filled = float(order.get('fill_quantity') or quantity)
            fill_price = order.get('fill_price')
            confirmed = bool(fill_price and fill_price > 0) or paper
            venue_entry = None
            if not confirmed:
                # The venue position is this row's alone (checked above): its size and average are
                # the truth for what the add filled.
                try:
                    want = 'Buy' if side == 'long' else 'Sell'
                    rows = [p for p in await self.bybit.get_positions(category='linear', symbol=symbol)
                            if p.get('side') == want]
                    venue_size = sum(float(p.get('size') or 0) for p in rows)
                    avg = float(rows[0].get('avgPrice') or 0) if len(rows) == 1 else 0.0
                    if venue_size > size and avg > 0:
                        filled, venue_entry, confirmed = venue_size - size, avg, True
                        logger.warning(f"[Increase] {symbol} #{position_id}: add fill read from the venue position "
                                       f"({filled} filled, average {avg})")
                except Exception as e:
                    logger.warning(f"[Increase] {symbol} #{position_id}: venue position unreadable after the add: {e}")
            if not fill_price or fill_price <= 0:
                fill_price = mark
                if not confirmed:
                    logger.error(f"[Increase] {symbol} #{position_id}: add fill UNCONFIRMED - recorded at the "
                                 f"expected {mark}; the position's PnL waits for the venue's records")
            meta = _json_obj(row.get('entry_meta'))
            if not confirmed:
                meta['entry_price_source'] = 'expected_unconfirmed'
            now = datetime.now(UTC)
            try:
                # From the row read under this position's lock (every writer of its size takes it).
                new_size = size + filled
                new_entry = venue_entry or (float(row['entry_price']) * size + float(fill_price) * filled) / new_size
                if venue_entry:
                    fill_price = (venue_entry * new_size - float(row['entry_price']) * size) / filled
                async with self.db.acquire() as conn, conn.transaction():
                    updated = await conn.fetchrow(
                        "UPDATE positions SET entry_price = $1, size = $2, entry_meta = $3, updated_at = $4 "
                        "WHERE id = $5 AND status = 'OPEN' RETURNING size, entry_price",
                        new_entry, new_size, json.dumps(meta), now, position_id)
                    if updated is None:
                        raise RuntimeError('the position is no longer open')
                    await conn.execute(
                        "INSERT INTO trades (position_id, symbol, trade_type, side, qty, price, time, order_id, "
                        "exit_reason, metadata, created_at, updated_at) "
                        "VALUES ($1, $2, 'entry', $3, $4, $5, $6, $7, NULL, $8, $6, $6)",
                        position_id, symbol, 'BUY' if side == 'long' else 'SELL', filled, float(fill_price), now,
                        None if paper else order.get('order_id'), json.dumps({'add': True, 'reason': reason}))
            except Exception as e:
                # The order filled: the venue position is larger than the record until the position
                # sync / import sees it. Loud, never silent.
                logger.critical(f"[Increase] {symbol} #{position_id}: added {filled} on the venue but NOT recorded "
                                f"({e}) - check the position")
                return refuse('not_recorded', f'added {filled} but not recorded ({e})', sizing=sizing)
            logger.info(f"[Increase] {symbol} {side} #{position_id}{' [paper]' if paper else ''}: +{filled} @ "
                        f"{fill_price} -> size {updated['size']}, entry {updated['entry_price']:.8g} ({reason})")
            return {'success': True, 'added_quantity': filled, 'fill_price': float(fill_price),
                    'size': float(updated['size']), 'entry_price': float(updated['entry_price']), 'sizing': sizing}

    async def execute_partial_exit(
        self,
        position: dict[str, Any],
        partial_pct: float,
        reason: str = 'reduce',
    ) -> dict[str, Any]:
        """
        Close part of a position (reduce-only), by % of its CURRENT size.

        Serialised per position, and the size is re-read inside the lock (two concurrent reduces
        must not both work from the same stale size). The partial is recorded as its own trade
        (trade_type 'partial_exit') with its PnL from the real fills, or PnL unknown when the venue
        has not confirmed the fill; the full close later includes it. A reduce that would close
        everything is a full close.

        Returns {'success', 'exit_price', 'partial_qty', 'remaining_qty', 'partial_pnl', 'sizing'}
        or {'success': False, 'error', 'sizing'?}.
        """
        symbol = position['symbol']
        position_id = position['id']

        async with self._position_lock(position_id):
            current = await self._fresh_row(position_id)
            if current is None or current['status'] != 'OPEN':
                return {'success': False, 'error': f'position {position_id} is not open'}
            total_size = float(current['size'])

            requested_qty = total_size * (partial_pct / 100)
            partial_qty = requested_qty
            qty_step = None

            # Lot-size rounding (DOWN: never close more than asked) and min-qty validation. Unlike an
            # entry, unknown lot rules do not block a reduction: it is attempted with the raw qty.
            try:
                specs = await self._instrument_specs(symbol)
                if specs:
                    qty_step = specs['qty_step']
                    min_qty = specs['min_qty']
                    partial_qty = round_qty_to_step(requested_qty, qty_step)
                    if partial_qty < min_qty or partial_qty <= 0:
                        logger.warning(
                            f"[PartialExit] {symbol}: {partial_pct:g}% of {total_size} is {requested_qty:.8f}, "
                            f"below the venue minimum {min_qty} (step {qty_step}) - SKIPPED"
                        )
                        return {
                            'success': False,
                            'error': (f'{partial_pct:g}% of {total_size} is {requested_qty:.8f}, below the '
                                      f'venue minimum {min_qty} (quantity step {qty_step})'),
                            'sizing': {'requested_quantity': requested_qty, 'quantity': 0.0,
                                       'qty_step': qty_step, 'min_qty': min_qty},
                        }
            except Exception as e:
                logger.warning(f"[PartialExit] {symbol}: Lot size validation failed, using raw qty: {e}")

            if partial_qty >= total_size - 1e-12:
                logger.info(f"[PartialExit] {symbol}: {partial_pct:g}% is the whole position - full close")
                try:
                    status = await self._execute_exit_locked(position, reason)   # already holding the lock
                except Exception as e:
                    return {'success': False, 'error': humanize_order_error(str(e))}
                if status == 'in_progress':
                    return {'success': False, 'error': 'another request is already closing this position'}
                return {'success': True, 'closed_fully': True, 'partial_qty': total_size, 'remaining_qty': 0.0}

            reduce_sizing = {
                'requested_quantity': requested_qty,
                'quantity': partial_qty,
                'shortfall_pct': round((requested_qty - partial_qty) / requested_qty * 100, 4) if requested_qty else 0.0,
                'adjustment': 'lot_step' if partial_qty < requested_qty else None,
                'qty_step': qty_step,
            }
            logger.info(
                f"[PartialExit] {symbol}: Closing {partial_pct:.1f}% ({partial_qty}) of "
                f"{position['side']} position (reason: {reason})"
            )

            try:
                partial_result = await self.order_executor.close_position(
                    symbol=symbol,
                    side=current['side'],
                    quantity=partial_qty,
                    simulate=_position_mode(current),
                )
                if not partial_result['success']:
                    raw_error = partial_result.get('error', 'Unknown error')
                    logger.error(f"[PartialExit] {symbol}: Partial close failed - {raw_error}")
                    return {'success': False, 'error': humanize_order_error(raw_error)}

                closed_qty = float(partial_result.get('fill_quantity') or partial_qty)
                exit_price = partial_result.get('fill_price')      # None = not confirmed by the venue
                paper = str(partial_result.get('order_id') or '').startswith('dry_run_')
                async with self.db.acquire() as conn:
                    partial_pnl = await self._record_partial(conn, current, closed_qty, exit_price,
                                                             partial_result.get('order_id'), reason, paper)
                remaining_qty = total_size - closed_qty
                logger.info(
                    f"[PartialExit] {symbol}: ✓ closed {closed_qty} @ {exit_price or 'unconfirmed price'}, "
                    f"remaining {remaining_qty} (partial PnL: {partial_pnl})"
                )
                return {
                    'success': True,
                    'exit_price': exit_price,
                    'partial_qty': closed_qty,
                    'remaining_qty': remaining_qty,
                    'partial_pnl': partial_pnl,
                    'sizing': reduce_sizing,
                }

            except Exception as e:
                error_str = str(e)
                logger.opt(exception=True).error(f"[PartialExit] {symbol}: Partial exit failed - {error_str}")
                return {'success': False, 'error': humanize_order_error(error_str)}

    async def _stop_change_context(self, position_id: int) -> tuple[dict | None, dict | None]:
        """The checks every stop change on an open position shares (call it holding the position's
        lock): (context, None) with the fresh row, side, entry, mode, mark price and tick size, or
        (None, {'code', 'error'}) with code not_open / shared_venue_position / no_price."""
        row = await self._fresh_row(position_id)
        if row is None or row['status'] != 'OPEN' or self._close_owned(position_id):
            return None, {'code': 'not_open', 'error': f'position {position_id} is not open (or is being closed)'}
        symbol, side = row['symbol'], row['side'].lower()
        paper = bool(row.get('dry_run'))
        if not paper:
            scope_sql, scope_args = self._account_scope_sql(4)
            async with self.db.acquire() as conn:
                shared = await conn.fetchval(
                    "SELECT count(*) FROM positions WHERE symbol = $1 AND side = $2 AND id <> $3 "
                    "AND status IN ('OPEN', 'CLOSING')" + scope_sql,
                    symbol, row['side'], position_id, *scope_args)
            if shared:
                return None, {'code': 'shared_venue_position',
                              'error': f'{symbol} {side} is one position on the venue shared with {shared} other '
                                       f'open row(s); its stops are theirs too - change them on the venue or '
                                       f'close one'}
        try:
            ticker = await self.bybit.get_ticker(symbol=symbol, category='linear')
            mark = float(ticker.get('markPrice') or ticker.get('lastPrice') or 0)
        except Exception as e:
            return None, {'code': 'no_price', 'error': f'{symbol}: no current price ({e}) - nothing changed'}
        if mark <= 0:
            return None, {'code': 'no_price', 'error': f'{symbol}: no current price - nothing changed'}
        specs = await self._instrument_specs(symbol)
        return {'row': row, 'side': side, 'entry': float(row['entry_price']), 'paper': paper, 'mark': mark,
                'tick': specs['tick_size'] if specs else None}, None

    async def set_trailing(self, position: dict[str, Any], distance_pct: float | None,
                           activate_pct: float | None = None, reason: str = 'api') -> dict[str, Any]:
        """
        Set an open position's trailing stop, or remove it (distance_pct None or 0).

        The trailing stop follows the best price at distance_pct % (of the price it starts from: the
        activation price, else the current price; tick-aligned, at least one tick). With
        activate_pct it starts once the position is that % in profit from entry; if the price is
        already there it trails at once (reported). It runs BESIDE the fixed stop, which stays in
        place as the floor (on Bybit both are separate orders; whichever is hit first closes).
        Recorded in positions.management['trailing']. A paper position's trailing stop is only
        recorded. Same refusals as move_stop (not_open, shared_venue_position, no_price,
        venue_rejected), plus 'invalid' for a distance outside 0-100% or a negative activation.

        Returns {'success': True, 'trailing': {...} or None (removed), 'warnings'} or
        {'success': False, 'code', 'error'}.
        """
        position_id = position['id']

        def refuse(code: str, error: str) -> dict[str, Any]:
            logger.warning(f"[Trailing] position {position_id}: {error}")
            return {'success': False, 'code': code, 'error': error}

        problem = trailing_problem(distance_pct, activate_pct)
        if problem:
            return refuse('invalid', problem)
        async with self._position_lock(position_id):
            ctx, refusal = await self._stop_change_context(position_id)
            if refusal:
                return refuse(refusal['code'], refusal['error'])
            row, side, entry, mark, tick = ctx['row'], ctx['side'], ctx['entry'], ctx['mark'], ctx['tick']
            symbol, long_side = row['symbol'], ctx['side'] == 'long'
            warnings: list[str] = []
            plan = None
            if distance_pct:
                active_price = None
                if activate_pct:
                    target = entry * (1 + activate_pct / 100) if long_side else entry * (1 - activate_pct / 100)
                    active_price = round_price_to_tick(target, tick)
                    if active_price <= 0:
                        return refuse('invalid', f'a {activate_pct:g}% activation would put the price at '
                                                 f'{active_price:g} for a short - it must stay above 0')
                    if (mark >= active_price) if long_side else (mark <= active_price):
                        warnings.append(f'already {activate_pct:g}% in profit: trails from now')
                        active_price = None
                base = active_price or mark
                distance = base * distance_pct / 100
                if tick:
                    distance = max(tick, round_price_to_tick(distance, tick))
                plan = {'distance_pct': distance_pct, 'activate_pct': activate_pct or None,
                        'distance': distance, 'active_price': active_price}
                res = await self.stop_loss_manager.set_trailing_stop(
                    symbol, side, distance, active_price, simulate=_position_mode(row))
                # Stamped once the venue holds it: the sync drops a record only if it was set
                # before the venue was read (it cannot be in that reading yet otherwise).
                plan['set_at'] = datetime.now(UTC).isoformat()
            else:
                res = await self.stop_loss_manager.set_trailing_stop(symbol, side, 0, simulate=_position_mode(row))
            if not res.get('success'):
                return refuse('venue_rejected', f'{symbol}: the venue refused the trailing stop: '
                                                f'{humanize_order_error(str(res.get("error")))}')

            def change(m: dict) -> dict:
                if plan:
                    m['trailing'] = plan
                else:
                    m.pop('trailing', None)
                return m
            try:
                await self._write_management(position_id, change)
            except Exception as e:
                logger.error(f"[Trailing] {symbol} #{position_id}: trailing stop set but not recorded: {e}")
                warnings.append(f'set but not recorded ({e})')
            logger.info(f"[Trailing] {symbol} {side} #{position_id}{' [paper]' if ctx['paper'] else ''}: "
                        f"{plan or 'removed'} (reason: {reason})")
            return {'success': True, 'trailing': plan, 'warnings': warnings}

    async def _write_management(self, position_id: int, change) -> dict | None:
        """Apply `change(management) -> management` to the fresh record (call holding the position's
        lock). Returns the new value; None if the position is no longer open."""
        row = await self._fresh_row(position_id)
        if row is None or row['status'] != 'OPEN':
            return None
        management = change(_json_obj(row.get('management')))
        async with self.db.acquire() as conn:
            await conn.execute(
                "UPDATE positions SET management = $1, updated_at = $2 WHERE id = $3 AND status = 'OPEN'",
                json.dumps(management) if management else None, datetime.now(UTC), position_id)
        return management

    async def set_take_profits(self, position: dict[str, Any], levels: list[dict[str, Any]] | None,
                               reason: str = 'api') -> dict[str, Any]:
        """
        Put a take-profit ladder on an open position (replacing any earlier one), or remove it
        (levels None / empty).

        Each level (relay.execution.take_profit_ladder) closes its close_pct of the CURRENT size at
        its price. On a live position each level is a reduce-only limit order resting on the venue
        (tagged with the relay's order link); a paper position's levels are filled by the relay when
        the price reaches them (run_stop_rules). A level the price has already passed, or whose
        share is below the venue minimum, is 'skipped'; one the venue refuses is 'failed'; both are
        reported, the rest is placed. A whole-position take-profit set at entry is removed (a
        ladder replaces it). The fixed stop, trailing stop and break-even are untouched.
        Recorded in positions.management['take_profits']. Same refusals as move_stop, plus
        'invalid' (unusable levels, or levels on the wrong side of entry).

        Returns {'success': True, 'take_profits': [levels] or None, 'warnings'} or
        {'success': False, 'code', 'error'}.
        """
        position_id = position['id']

        def refuse(code: str, error: str) -> dict[str, Any]:
            logger.warning(f"[TakeProfit] position {position_id}: {error}")
            return {'success': False, 'code': code, 'error': error}

        if levels:
            problem = ladder_problem(levels)
            if problem:
                return refuse('invalid', problem)
        async with self._position_lock(position_id):
            ctx, refusal = await self._stop_change_context(position_id)
            if refusal:
                return refuse(refusal['code'], refusal['error'])
            row, side, entry, mark = ctx['row'], ctx['side'], ctx['entry'], ctx['mark']
            symbol, long_side, paper = row['symbol'], ctx['side'] == 'long', ctx['paper']
            if levels:
                problem = ladder_price_problem(side, entry, levels)
                if problem:
                    return refuse('invalid', problem)
            warnings: list[str] = []
            # The earlier ladder's orders go first: two ladders must never rest at once. One that
            # cannot be cancelled may just have filled: refuse, and the next pass records it.
            management = _json_obj(row.get('management'))
            old = (management.get('take_profits') or {}).get('levels') or []
            await self._cancel_ladder_orders(symbol, old, paper, 'replaced')
            # What part-filled before its cancel is recorded first: the new ladder is sized on
            # what is left.
            still_open, _ = await self._record_cancelled_fills(row, management, old, paper)
            if not still_open:
                return refuse('not_open', f'position {position_id} closed while its take-profits were replaced')
            row = await self._fresh_row(position_id)
            stuck = [lv['n'] for lv in old if lv.get('status') == 'open']
            if stuck:
                def keep_old(m: dict) -> dict:
                    if 'take_profits' in m:
                        m['take_profits']['levels'] = old
                    return m
                await self._write_management(position_id, keep_old)
                return refuse('venue_rejected', f'take-profit level(s) {stuck} could not be cancelled, or what '
                                                f'filled is not readable yet - the rest is cancelled; try again '
                                                f'in a moment')

            plan = None
            if levels:
                specs = await self._instrument_specs(symbol)
                plan = plan_ladder(side, entry, float(row['size']), levels,
                                   specs['tick_size'] if specs else None,
                                   specs['qty_step'] if specs else None, specs['min_qty'] if specs else None)
                if not paper:
                    await self._remove_whole_position_take_profit(row)
                for lv in plan:
                    if lv['status'] != 'planned':
                        warnings.append(f"level {lv['n']}: {lv['note']}")
                        continue
                    if (mark >= lv['price']) if long_side else (mark <= lv['price']):
                        lv.update(status='skipped', qty=0.0, note=f'the price {mark:g} has already passed it')
                        warnings.append(f"level {lv['n']}: {lv['note']}")
                        continue
                    if paper:
                        lv.update(status='open', order_id=f"dry_run_tp{lv['n']}_{symbol}")
                        continue
                    link = make_order_link(self.deployment_id)
                    lv['order_link_id'] = link
                    try:
                        res = await self.bybit.place_limit_order(
                            symbol=symbol, side='Sell' if long_side else 'Buy',
                            qty=format_qty(lv['qty'], specs['qty_step'] if specs else None),
                            price=format_price(lv['price'], specs['tick_size'] if specs else None),
                            post_only=False, reduce_only=True, order_link_id=link)
                        lv.update(status='open', order_id=res.get('orderId'))
                    except Exception as e:
                        placed = await self._placed_despite_error(symbol, link)
                        if placed:
                            # The request failed on the way back, but the venue has the order: it
                            # is tracked like any other (a fill is recorded by the next pass).
                            lv.update(status='open', order_id=placed)
                            continue
                        lv.update(status='failed', note=humanize_order_error(str(e)))
                        warnings.append(f"level {lv['n']}: the venue refused it: {lv['note']}")
                for lv in plan:
                    lv.setdefault('order_id', None)
                    lv.update(filled_qty=0.0, fill_price=None)

            def change(m: dict) -> dict:
                if plan:
                    m['take_profits'] = {'levels': plan, 'set_at': datetime.now(UTC).isoformat()}
                else:
                    m.pop('take_profits', None)
                return m
            try:
                await self._write_management(position_id, change)
            except Exception as e:
                # Orders that rest without a record would never be tracked or cleaned up.
                if plan:
                    await self._cancel_ladder_orders(symbol, plan, paper, 'not recorded')
                return refuse('not_recorded', f'take-profit ladder not recorded ({e}) - its orders were cancelled')
            logger.info(f"[TakeProfit] {symbol} {side} #{position_id}{' [paper]' if paper else ''}: "
                        f"{[(lv['n'], lv['price'], lv['qty'], lv['status']) for lv in plan] if plan else 'removed'} "
                        f"(reason: {reason})")
            return {'success': True, 'take_profits': plan, 'warnings': warnings}

    async def _placed_despite_error(self, symbol: str, link: str) -> str | None:
        """After a failed placement: the order id if the venue has the order anyway (the answer was
        lost, e.g. a timeout). Read in real time (the order history does not list resting orders).
        If that cannot be read, the order is cancelled by its link, so nothing the relay does not
        track can rest."""
        try:
            o = await self.bybit.get_order(symbol=symbol, order_link_id=link)
            if o is None:
                return None
            if o.get('orderStatus') in _ORDER_DONE and not float(o.get('cumExecQty') or 0):
                return None                     # rejected or cancelled, nothing filled
            return o.get('orderId') or None
        except Exception as e:
            logger.warning(f"[TakeProfit] {symbol}: order {link} unreadable after a failed placement ({e}) - "
                           f"cancelling it in case it rests")
            try:
                await self.bybit.cancel_order(symbol=symbol, order_link_id=link)
            except Exception as e2:
                logger.error(f"[TakeProfit] {symbol}: order {link} could not be cancelled ({e2}) - check the "
                             f"venue for a resting reduce-only order")
            return None

    async def _remove_whole_position_take_profit(self, row: dict[str, Any]) -> None:
        """A ladder replaces a whole-position take-profit set at entry (it would close everything)."""
        async with self.db.acquire() as conn:
            tp = await conn.fetchval(
                "SELECT take_profit_price FROM trades WHERE position_id = $1 AND trade_type = 'entry' "
                "AND take_profit_price IS NOT NULL ORDER BY time DESC LIMIT 1", row['id'])
        if tp:
            res = await self.stop_loss_manager.place_take_profit(row['symbol'], row['side'].lower(), 0,
                                                                 simulate=_position_mode(row))
            if not res.get('success'):
                logger.warning(f"[TakeProfit] {row['symbol']} #{row['id']}: the whole-position take-profit "
                               f"{tp} could not be removed: {res.get('error')}")

    async def _cancel_ladder_orders(self, symbol: str, levels: list[dict[str, Any]], paper: bool,
                                    why: str, settle_gone: bool = False) -> None:
        """Cancel the resting orders of `levels` still open, marking them 'cancelled'. One the venue
        will not cancel stays 'open' (it may have filled: the fill check records it), unless
        `settle_gone` (a closed position, nothing left to record): then an order the venue shows as
        filled or cancelled is marked 'gone'.

        A live order that had part filled before the cancel is marked 'cancelled' with its
        filled_qty / fill_price and 'unrecorded': the caller records that part
        (_record_cancelled_fills). One whose fill cannot be read after the cancel stays 'open': the
        next pass reads it (it is no longer resting) and records what filled."""
        for lv in levels:
            if lv.get('status') != 'open':
                continue
            if not paper and lv.get('order_id'):
                try:
                    await self.bybit.cancel_order(symbol=symbol, order_id=lv['order_id'])
                except Exception as e:
                    # "Order not exists" (Bybit cancels a closed position's reduce-only orders
                    # itself, verified on the demo venue) or a venue that shows it done: gone.
                    if settle_gone and (is_benign_order_state(str(e))
                                        or await self._order_is_done(symbol, lv['order_id'])):
                        lv.update(status='gone', note='already filled or cancelled on the venue')
                        continue
                    logger.info(f"[TakeProfit] {symbol}: level {lv.get('n')} order {lv['order_id']} not "
                                f"cancelled ({e}) - checked again next pass")
                    continue
                if not settle_gone:
                    fill = await self._order_fill(symbol, lv['order_id'])
                    if fill is None or not fill[0]:
                        logger.info(f"[TakeProfit] {symbol}: level {lv.get('n')} cancelled, but what had "
                                    f"filled is not readable yet - recorded next pass")
                        continue
                    if fill[1] > 0:
                        lv.update(status='cancelled', note=f'{why} (part had filled)', filled_qty=fill[1],
                                  fill_price=fill[2], unrecorded=True)
                        continue
            lv.update(status='cancelled', note=why)

    async def _record_cancelled_fills(self, row: dict[str, Any], management: dict[str, Any],
                                      levels: list[dict[str, Any]], paper: bool) -> tuple[bool, float]:
        """Record the part-fills of levels _cancel_ladder_orders marked 'unrecorded' (call holding
        the position's lock; `levels` belong to `management`, which is saved with each record).
        Returns (still open, quantity recorded)."""
        recorded_qty = 0.0
        for lv in levels:
            if not lv.pop('unrecorded', False):
                continue
            async with self.db.acquire() as conn:
                recorded, _ = await self._record_partial_tx(conn, row, float(lv['filled_qty']), lv.get('fill_price'),
                                                            lv.get('order_id'), 'take_profit', paper,
                                                            management=management)
            if not recorded:
                return False, recorded_qty
            recorded_qty += float(lv['filled_qty'])
        return True, recorded_qty

    async def _order_fill(self, symbol: str, order_id: str) -> tuple[bool, float, float | None] | None:
        """(no longer resting, quantity filled, average fill price) of a venue order; None when the
        venue's answer cannot be read. The real-time lookup knows an order's final state at once;
        the order history (which lags by seconds) only for an order the venue no longer shows
        there."""
        try:
            o = await self.bybit.get_order(symbol=symbol, order_id=order_id)
            if o is None:
                hist = await self.bybit.get_order_history(symbol=symbol, order_id=order_id)
                o = next((h for h in hist or [] if h.get('orderId') == order_id), None)
            if o is None:
                return False, 0.0, None
            return (o.get('orderStatus') in _ORDER_DONE, float(o.get('cumExecQty') or 0),
                    float(o.get('avgPrice') or 0) or None)
        except Exception as e:
            logger.warning(f"[TakeProfit] {symbol}: order {order_id} unreadable ({e}) - checked next pass")
            return None

    async def _order_is_done(self, symbol: str, order_id: str) -> bool:
        """True if the venue shows the order as no longer resting (filled, cancelled, rejected)."""
        fill = await self._order_fill(symbol, order_id)
        return bool(fill and fill[0])

    async def _cancel_leftover_take_profits(self, symbol: str | None = None) -> tuple[int, int]:
        """Cancel ladder orders of this deployment's CLOSED positions that are still marked open (a
        close the relay did not place itself, e.g. the stop, leaves them resting). Reduce-only, they
        cannot open a position, but they would reduce the NEXT position on the symbol. Returns
        (positions cleaned, levels still open: not cancelled and not known to be done)."""
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, symbol, dry_run, management FROM positions WHERE status = 'CLOSED' "
                "AND management IS NOT NULL AND coalesce(deployment_id, -1) = coalesce($1, -1) "
                "AND closed_at > $2" + (" AND symbol = $3" if symbol else ""),
                self.deployment_id, datetime.now(UTC) - timedelta(days=7), *([symbol] if symbol else []))
        cleaned = stuck = 0
        for r in rows:
            management = _json_obj(r['management'])
            levels = (management.get('take_profits') or {}).get('levels') or []
            if not any(lv.get('status') == 'open' for lv in levels):
                continue
            await self._cancel_ladder_orders(r['symbol'], levels, bool(r['dry_run']), 'position closed',
                                             settle_gone=True)
            async with self.db.acquire() as conn:
                await conn.execute("UPDATE positions SET management = $1 WHERE id = $2",
                                   json.dumps(management), r['id'])
            cleaned += 1
            stuck += sum(lv.get('status') == 'open' for lv in levels)
        return cleaned, stuck

    async def _check_take_profits(self, p: dict[str, Any], prices: dict, out: dict[str, Any],
                                  open_orders: dict) -> None:
        """One position's ladder: record the levels that filled (live: from the venue's order
        status; paper: when the mark price reached them), and shrink resting orders that would
        close more than the position still holds (e.g. after a manual reduce). Each fill is recorded
        together with the level's new state, so no fill is ever recorded twice."""
        symbol, long_side = p['symbol'], p['side'].lower() == 'long'
        paper = bool(p.get('dry_run'))
        mark = await self._mark(symbol, prices)
        if not paper and symbol not in open_orders:
            try:
                open_orders[symbol] = {o.get('orderId') for o in await self.bybit.get_open_orders(symbol=symbol)}
            except Exception as e:
                logger.warning(f"[TakeProfit] {symbol}: open orders unreadable ({e}) - checked next pass")
                open_orders[symbol] = None
        resting = open_orders.get(symbol)
        close_fully = False
        async with self._position_lock(p['id']):
            row = await self._fresh_row(p['id'])
            if row is None or row['status'] != 'OPEN' or self._close_owned(p['id']):
                return
            management = _json_obj(row.get('management'))
            levels = (management.get('take_profits') or {}).get('levels') or []
            size = float(row['size'])
            changed = False
            for lv in levels:
                if lv.get('status') != 'open':
                    continue
                filled_qty, fill_price, done = 0.0, None, False
                if paper:
                    if mark is not None and ((mark >= lv['price']) if long_side else (mark <= lv['price'])):
                        filled_qty, fill_price, done = float(lv['qty']), float(lv['price']), True
                elif resting is not None and lv.get('order_id') not in resting:
                    fill = await self._order_fill(symbol, lv['order_id'])
                    if fill is not None:
                        done, filled_qty, fill_price = fill
                if not done:
                    continue
                if filled_qty <= 0:
                    lv.update(status='cancelled', note='cancelled on the venue')
                    out['tp_cancelled'].append(p['id'])
                    changed = True
                    continue
                lv.update(status='filled', filled_qty=filled_qty, fill_price=fill_price,
                          filled_at=datetime.now(UTC).isoformat())
                out['tp_filled'].append((p['id'], lv['n']))
                if filled_qty < size - 1e-12:
                    async with self.db.acquire() as conn:
                        recorded, _ = await self._record_partial_tx(conn, row, filled_qty, fill_price,
                                                                    lv.get('order_id'), 'take_profit', paper,
                                                                    management=management)
                    if not recorded:
                        return          # closed meanwhile: its close counts this; cleanup takes the rest
                    size -= filled_qty
                else:
                    changed = True
                    if paper:
                        close_fully = True    # the last of a paper position: a full close, below
                # A live level that took the whole position: the venue closed it; the position
                # sync / live feed records that close from the venue's records.
            changed |= await self._fit_ladder_to_size(symbol, levels, size, paper)
            still_open, _ = await self._record_cancelled_fills(row, management, levels, paper)
            if changed and still_open:
                def change(m: dict) -> dict:
                    if 'take_profits' in m:
                        m['take_profits']['levels'] = levels
                    return m
                await self._write_management(p['id'], change)
        if close_fully:
            await self.execute_exit(p, reason='take_profit')

    async def _fit_ladder_to_size(self, symbol: str, levels: list[dict[str, Any]], size: float,
                                  paper: bool) -> bool:
        """Shrink the furthest resting levels until they close no more than `size`. Returns True if
        anything changed. (A level cancelled after part had filled is left 'unrecorded' for the
        caller.)"""
        excess = sum(float(lv['qty']) for lv in levels if lv.get('status') == 'open') - size
        if excess <= 1e-12:
            return False
        specs = await self._instrument_specs(symbol)
        step, min_qty = (specs['qty_step'], specs['min_qty']) if specs else (None, 0)
        for lv in sorted((lv for lv in levels if lv.get('status') == 'open'), key=lambda x: -x['n']):
            if excess <= 1e-12:
                break
            keep = round_qty_to_step(float(lv['qty']) - excess, step)
            if keep <= 0 or (min_qty and keep < min_qty):
                await self._cancel_ladder_orders(symbol, [lv], paper, 'the position is smaller than the ladder')
                if lv.get('status') == 'cancelled':
                    excess -= float(lv['qty']) - float(lv.get('filled_qty') or 0)
                continue
            if not paper:
                try:
                    await self.bybit.amend_order(symbol=symbol, order_id=lv['order_id'],
                                                 qty=format_qty(keep, step))
                except Exception as e:
                    logger.warning(f"[TakeProfit] {symbol}: level {lv['n']} not resized ({e}) - next pass")
                    continue
            excess -= float(lv['qty']) - keep
            lv.update(qty=keep, note='resized to the position')
        return True

    async def _mark(self, symbol: str, prices: dict) -> float | None:
        """The mark price, read once per pass and symbol (None = unavailable)."""
        if symbol not in prices:
            try:
                t = await self.bybit.get_ticker(symbol=symbol, category='linear')
                prices[symbol] = float(t.get('markPrice') or t.get('lastPrice') or 0) or None
            except Exception as e:
                logger.warning(f"[StopRules] {symbol}: no price ({e}) - checked again next pass")
                prices[symbol] = None
        return prices[symbol]

    async def set_break_even(self, position: dict[str, Any], trigger_pct: float | None = None,
                             trigger_r: float | None = None, buffer_pct: float | None = None,
                             reason: str = 'api', trigger_tp: int | None = None) -> dict[str, Any]:
        """
        Arm break-even on an open position, or remove it (no trigger).

        Once the position is trigger_pct % in profit (mark price), or trigger_r times its initial
        risk (entry to the opening stop), or take-profit level trigger_tp has filled (1 = the
        first; the position needs a take-profit ladder with that level), the relay moves its stop to the entry price plus
        buffer_pct % on the profit side (run_stop_rules, every stop_rules_interval_s). The move goes
        through move_stop, so it is checked like any stop move, and it only ever tightens: a stop
        already at or past the target is left alone. Works for paper positions too (the relay
        watches the price itself; the venue has no break-even of its own).
        Recorded in positions.management['break_even']; re-arming replaces it. Refusals as for
        move_stop, plus 'invalid' (unusable inputs, an R trigger without an opening stop, a target
        not between entry and trigger).

        Returns {'success': True, 'break_even': {...} or None (removed), 'warnings'} or
        {'success': False, 'code', 'error'}.
        """
        position_id = position['id']

        def refuse(code: str, error: str) -> dict[str, Any]:
            logger.warning(f"[BreakEven] position {position_id}: {error}")
            return {'success': False, 'code': code, 'error': error}

        problem = break_even_problem(trigger_pct, trigger_r, buffer_pct, trigger_tp)
        if problem:
            return refuse('invalid', problem)
        async with self._position_lock(position_id):
            ctx, refusal = await self._stop_change_context(position_id)
            if refusal:
                return refuse(refusal['code'], refusal['error'])
            row, side, entry = ctx['row'], ctx['side'], ctx['entry']
            plan = None
            warnings: list[str] = []
            if trigger_pct is not None or trigger_r is not None or trigger_tp is not None:
                plan = {'trigger_pct': trigger_pct, 'trigger_r': trigger_r, 'buffer_pct': buffer_pct}
                if trigger_tp is not None:
                    plan['trigger_tp'] = trigger_tp
                    levels = (_json_obj(row.get('management')).get('take_profits') or {}).get('levels') or []
                    if len(levels) < trigger_tp:
                        return refuse('invalid', f'take-profit level {trigger_tp} does not exist on this '
                                                 f'position ({len(levels)} level(s))')
                try:
                    trigger, target = break_even_levels(side, entry, row.get('initial_stop_price'), plan,
                                                        ctx['tick'])
                except ValueError as e:
                    return refuse('invalid', str(e))
                plan.update(trigger_price=trigger, stop_target=target,
                            armed_at=datetime.now(UTC).isoformat(), done_at=None)
                if trigger is not None and ((ctx['mark'] >= trigger) if side == 'long' else (ctx['mark'] <= trigger)):
                    warnings.append('the trigger is already reached: the stop moves on the next check')

            def change(m: dict) -> dict:
                if plan:
                    m['break_even'] = plan
                else:
                    m.pop('break_even', None)
                return m
            try:
                await self._write_management(position_id, change)
            except Exception as e:
                return refuse('not_recorded', f'break-even not recorded ({e})')
            logger.info(f"[BreakEven] {row['symbol']} {side} #{position_id}{' [paper]' if ctx['paper'] else ''}: "
                        f"{plan or 'removed'} (reason: {reason})")
            return {'success': True, 'break_even': plan, 'warnings': warnings}

    async def run_stop_rules(self) -> dict[str, Any]:
        """
        One pass of the relay-side stop rules over this deployment's open positions (paper and
        live): take-profit levels that filled are recorded (paper: filled when the price reached
        them), resting levels larger than the position are shrunk, closed positions' leftover
        levels are cancelled; positions past the time stop are closed; then a break-even whose
        trigger is reached moves the stop (move_stop). A move the checks refuse (e.g. the price
        already fell back through the target) stays armed and is tried again on the next pass.

        Returns {'checked', 'moved': [ids], 'already_tighter': [ids], 'waiting', 'refused':
        [{'id', 'code', 'error'}], 'tp_filled': [(id, level)], 'tp_cancelled': [ids], 'tp_cleaned',
        'time_stopped': [ids]}.
        """
        async with self._stop_rules_lock:      # one pass at a time (the loop and POST /v1/stop-rules)
            return await self._run_stop_rules()

    async def _run_stop_rules(self) -> dict[str, Any]:
        out: dict[str, Any] = {'checked': 0, 'moved': [], 'already_tighter': [], 'waiting': 0, 'refused': [],
                               'tp_filled': [], 'tp_cancelled': [], 'tp_cleaned': 0, 'time_stopped': []}
        prices: dict[str, float | None] = {}
        open_orders: dict[str, set | None] = {}
        # Take-profit levels first: a filled level can be what arms break-even.
        for p in await self._own_open_positions(all_modes=True):
            levels = (_json_obj(p.get('management')).get('take_profits') or {}).get('levels') or []
            if p.get('status') != 'OPEN' or not any(lv.get('status') == 'open' for lv in levels):
                continue
            try:
                await self._check_take_profits(p, prices, out, open_orders)
            except Exception as e:
                logger.opt(exception=True).error(f"[TakeProfit] position {p['id']}: check failed: {e}")
                out['refused'].append({'id': p['id'], 'code': 'error', 'error': str(e)})
        try:
            out['tp_cleaned'], _ = await self._cancel_leftover_take_profits()
        except Exception as e:
            logger.error(f"[TakeProfit] cleanup of closed positions failed: {e}")
        await self._check_time_stops(out)
        for p in await self._own_open_positions(all_modes=True):
            plan = _json_obj(p.get('management')).get('break_even')
            if p.get('status') != 'OPEN' or not plan or plan.get('done_at') or self._close_owned(p['id']):
                continue
            out['checked'] += 1
            try:
                await self._check_break_even(p, plan, prices, out)
            except Exception as e:
                logger.opt(exception=True).error(f"[BreakEven] position {p['id']}: check failed: {e}")
                out['refused'].append({'id': p['id'], 'code': 'error', 'error': str(e)})
        return out

    async def _check_time_stops(self, out: dict[str, Any]) -> None:
        """The time stop: a position open for max_hold_hours (empty = off) is closed at market, paper
        or live. It counts from when the relay opened the position, or imported it from the venue.
        A close that fails is tried again on the next pass, and reported once."""
        try:
            hours = (await self._get_execution_settings()).get('max_hold_hours')
        except Exception as e:
            logger.error(f"[TimeStop] settings could not be read: {e}")
            return
        if not hours:
            return
        cutoff = datetime.now(UTC) - timedelta(hours=float(hours))
        for p in await self._own_open_positions(all_modes=True):
            opened = p.get('opened_at')
            if opened is not None and opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            if p.get('status') != 'OPEN' or opened is None or opened > cutoff or self._close_owned(p['id']):
                continue
            try:
                done = await self.execute_exit(p, reason='time_stop')
            except Exception as e:
                logger.opt(exception=True).error(f"[TimeStop] {p['symbol']} #{p['id']}: close failed: {e}")
                out['refused'].append({'id': p['id'], 'code': 'time_stop_failed', 'error': str(e)})
                if p['id'] not in self._time_stop_failed:
                    self._time_stop_failed.add(p['id'])
                    await self._event('position.time_stop_failed', 'critical',
                                      f"{p['symbol']} has been open {hours:g} hours, but its close failed: "
                                      f"tried again every few seconds ({e})"[:500], {'error': str(e)[:300]}, p['id'])
                continue
            self._time_stop_failed.discard(p['id'])
            if done == 'closed':
                out['time_stopped'].append(p['id'])
                logger.info(f"[TimeStop] {p['symbol']} #{p['id']}: closed after {hours:g} hours")

    async def _check_break_even(self, p: dict[str, Any], plan: dict[str, Any], prices: dict,
                                out: dict[str, Any]) -> None:
        symbol, side = p['symbol'], p['side'].lower()
        long_side = side == 'long'
        mark = await self._mark(symbol, prices)
        if mark is None:
            out['waiting'] += 1
            return
        specs = await self._instrument_specs(symbol)
        # From the current record: the entry price may have been confirmed since the plan was armed.
        trigger, target = break_even_levels(side, float(p['entry_price']), p.get('initial_stop_price'), plan,
                                            specs['tick_size'] if specs else None)
        if trigger is None:
            levels = (_json_obj(p.get('management')).get('take_profits') or {}).get('levels') or []
            n = plan['trigger_tp']
            reached = len(levels) >= n and levels[n - 1].get('status') == 'filled'
        else:
            reached = (mark >= trigger) if long_side else (mark <= trigger)
        if not reached:
            out['waiting'] += 1
            return
        # Only ever tightens: move_stop compares with the stop as it is under the position's lock
        # (the user, or the sync, may have moved it since this pass read the position).
        res = await self.move_stop(p, target, reason='break_even', only_tighten=True)
        if not res.get('success'):
            out['refused'].append({'id': p['id'], 'code': res.get('code'), 'error': res.get('error')})
            return
        if res['direction'] == 'already_tighter':
            await self._finish_break_even(p['id'], plan, res['stop_price'], moved=False)
            out['already_tighter'].append(p['id'])
            return
        await self._finish_break_even(p['id'], plan, res['stop_price'], moved=True)
        out['moved'].append(p['id'])
        logger.info(f"[BreakEven] {symbol} {side} #{p['id']}: "
                    + (f"price {mark:g} reached {trigger:g}" if trigger is not None
                       else f"take-profit level {plan['trigger_tp']} filled")
                    + f" - stop moved to {res['stop_price']:g}")

    async def _finish_break_even(self, position_id: int, plan: dict[str, Any], stop: float, moved: bool) -> None:
        def change(m: dict) -> dict:
            current = m.get('break_even')
            # Only the plan this pass acted on: one re-armed or removed meanwhile is left as it is.
            if current and current.get('armed_at') == plan.get('armed_at'):
                current.update(done_at=datetime.now(UTC).isoformat(), stop=stop, moved=moved)
            return m
        async with self._position_lock(position_id):
            await self._write_management(position_id, change)

    async def move_stop(self, position: dict[str, Any], stop_price: float, reason: str = 'api',
                        only_tighten: bool = False) -> dict[str, Any]:
        """
        Move an open position's stop to `stop_price` (tighter or looser) and record it as the
        position's current stop (positions.stop_price, which the stop audit re-arms).

        The stop is tick-aligned toward entry (rounding can only tighten it). Nothing is sent, and
        the old stop stays, when:
        - the position is not open, or is being closed                      -> 'not_open'
        - the mark price has already passed the new stop (it would fire at once)
                                                                            -> 'stop_already_breached'
        - the stop is on the loss side of entry and further from it than the user's optional
          max_stop_distance_pct                                             -> 'stop_too_far'
        - (live) another open row shares this venue position: the venue holds ONE stop per symbol
          and side, so moving it would move theirs too                      -> 'shared_venue_position'
        - no current price                                                   -> 'no_price'
        The venue refusing the stop is 'venue_rejected' (its stop is unchanged).
        A stop in profit (beyond entry) is allowed: that is how a stop locks in a gain.
        Serialised with closes and reduces of the same position; follows the position's own mode
        (a paper position's stop is only recorded). With only_tighten, a current stop that is
        already at or past `stop_price` (in the profit direction) is kept and nothing is sent.

        Returns {'success': True, 'stop_price', 'previous_stop', 'direction', 'warnings'} with
        direction 'tightened' / 'loosened' / 'unchanged' / 'set' (no previous stop) /
        'already_tighter' (only_tighten: kept), or {'success': False, 'code', 'error'}.
        """
        position_id = position['id']

        def refuse(code: str, error: str) -> dict[str, Any]:
            logger.warning(f"[MoveStop] position {position_id}: {error}")
            return {'success': False, 'code': code, 'error': error}

        async with self._position_lock(position_id):
            ctx, refusal = await self._stop_change_context(position_id)
            if refusal:
                return refuse(refusal['code'], refusal['error'])
            row, side, entry, paper, mark = ctx['row'], ctx['side'], ctx['entry'], ctx['paper'], ctx['mark']
            symbol = row['symbol']
            previous = row.get('stop_price') if row.get('stop_price') is not None else row.get('initial_stop_price')
            long_side = side == 'long'
            stop = round_stop_to_tick(float(stop_price), ctx['tick'], side)
            if only_tighten and previous is not None and (
                    (float(previous) >= stop) if long_side else (float(previous) <= stop)):
                return {'success': True, 'stop_price': float(previous), 'previous_stop': previous,
                        'direction': 'already_tighter', 'warnings': []}

            checked = self.stop_loss_manager.validate_stop(side, entry, stop, current_price=mark)
            if not checked['valid']:
                return refuse('stop_already_breached',
                              f'{symbol} {side}: the price {mark:g} has already passed the stop {stop:g}; '
                              f'it would fire at once - stop not moved')

            settings = await self._get_execution_settings()
            max_pct = settings.get('max_stop_distance_pct')
            loss_side = stop < entry if long_side else stop > entry
            distance_pct = abs(entry - stop) / entry * 100 if entry > 0 else 0.0
            if max_pct and loss_side and distance_pct > float(max_pct):
                return refuse('stop_too_far',
                              f'{symbol} {side}: stop {stop:g} is {distance_pct:.3f}% from entry {entry:g}, '
                              f'beyond your max_stop_distance_pct {float(max_pct):g}% - stop not moved')

            res = await self.stop_loss_manager.place_stop_loss(
                symbol, side, float(row['size']), stop, simulate=_position_mode(row))
            if not res.get('success'):
                return refuse('venue_rejected', f'{symbol}: the venue refused the stop {stop:g}: '
                                                f'{humanize_order_error(str(res.get("error")))}')

            warnings = []
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        "UPDATE positions SET stop_price = $1, updated_at = $2 WHERE id = $3 AND status = 'OPEN'",
                        stop, datetime.now(UTC), position_id)
            except Exception as e:
                # The venue holds the new stop; the position sync records it from there (live).
                logger.error(f"[MoveStop] {symbol} #{position_id}: stop set but not recorded: {e}")
                warnings.append(f'stop set but not recorded ({e})')

            if previous is None:
                direction = 'set'
            elif stop == float(previous):
                direction = 'unchanged'
            else:
                direction = 'tightened' if (stop > float(previous)) == long_side else 'loosened'
            if stop != float(stop_price):
                warnings.append(f'aligned to the price tick: {float(stop_price):g} -> {stop:g}')
            logger.info(f"[MoveStop] {symbol} {side} #{position_id}{' [paper]' if paper else ''}: stop "
                        f"{previous} -> {stop} ({direction}, reason: {reason})")
            return {'success': True, 'stop_price': stop, 'previous_stop': previous,
                    'direction': direction, 'warnings': warnings}

    async def _create_position_record(
        self,
        signal: dict[str, Any],
        order_result: dict[str, Any],
        quantity: float,
        stop_loss: dict[str, Any],
        stop_result: dict[str, Any] | None,
        take_profit_price: float | None,
        leverage: float,
        is_dry_run: bool,
    ) -> dict[str, Any]:
        """
        Create position and trade records in database with atomic signal linking.

        CRITICAL: All DB writes are in one transaction to prevent:
        - Orphaned positions (position created, signal update fails)
        - Orphaned signals (signal marked executed but position creation fails)
        """
        async with self.db.acquire() as conn:
            # Use explicit transaction for atomicity
            async with conn.transaction():
                now = datetime.now(UTC)

                # signals row id (may be None for manual entries)
                signal_id = signal.get('signal_id')
                entry_price = order_result['fill_price']
                initial_stop_price = stop_loss['stop_price']

                entry_meta = {
                    'signal_id': signal_id,
                    'reason': signal.get('reason'),
                    'requested_size': signal.get('size'),
                    'requested_stop': signal.get('stop_price'),
                    'expected_entry': signal.get('entry_price'),
                    'entry_order_id': order_result.get('order_id'),
                    'entry_price_source': order_result.get('fill_source'),
                }

                # First, create the position record.
                # ON CONFLICT prevents a duplicate if import_exchange_positions() raced us (the
                # order-link tag lets the importer attribute our own fill to this deployment).
                position = await conn.fetchrow(
                    """
                    INSERT INTO positions (
                        deployment_id, symbol, side, size, entry_price, status, dry_run, opened_at,
                        entry_meta, initial_stop_price, stop_price, leverage, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10, $11, $12, $13)
                    ON CONFLICT (coalesce(deployment_id, -1), symbol, side, dry_run) WHERE status = 'OPEN'
                    DO UPDATE SET
                        size = EXCLUDED.size,
                        entry_price = EXCLUDED.entry_price,
                        entry_meta = EXCLUDED.entry_meta,
                        initial_stop_price = EXCLUDED.initial_stop_price,
                        stop_price = EXCLUDED.stop_price,
                        leverage = EXCLUDED.leverage,
                        updated_at = EXCLUDED.updated_at
                    RETURNING *
                    """,
                    self.deployment_id,
                    signal['symbol'],
                    signal['direction'].upper(),
                    quantity,
                    entry_price,
                    'OPEN',
                    is_dry_run,
                    now,
                    json.dumps(entry_meta, default=str),
                    initial_stop_price,
                    leverage,
                    now,
                    now,
                )

                # Clean up any import-stub trade if import_exchange_positions()
                # raced us and created the position first (ON CONFLICT DO UPDATE above).
                # If no race occurred this is a no-op.
                await conn.execute(
                    """
                    DELETE FROM trades
                    WHERE position_id = $1 AND reason = $2
                    """,
                    position['id'],
                    IMPORT_TRADE_REASON,
                )

                # Then, create the entry trade record (linked to the position)
                trade = await conn.fetchrow(
                    """
                    INSERT INTO trades (
                        position_id, symbol, trade_type, side, order_id, price, time,
                        qty, stop_loss_price, stop_loss_order_id, take_profit_price,
                        metadata, reason, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    RETURNING *
                    """,
                    position['id'],  # Link trade to position
                    signal['symbol'],
                    'entry',
                    'BUY' if signal['direction'].lower() == 'long' else 'SELL',
                    order_result['order_id'],
                    order_result['fill_price'],
                    now,
                    quantity,
                    stop_loss['stop_price'],
                    stop_result.get('order_id') if stop_result else None,  # Safe access when stop_result is None
                    take_profit_price,
                    json.dumps({'dry_run': is_dry_run, 'signal_reason': signal.get('reason')}),
                    signal.get('reason', 'signal'),
                    now,  # created_at
                    now,  # updated_at
                )

                # Mark signal as executed with position_id (atomic - same transaction)
                if signal_id:
                    await conn.execute(
                        """
                        UPDATE signals
                        SET status = 'executed',
                            result = $1,
                            updated_at = $2
                        WHERE id = $3
                        """,
                        json.dumps({'position_id': position['id'], 'fill_price': entry_price,
                                    'dry_run': is_dry_run}),
                        now,
                        signal_id,
                    )
                    logger.debug(
                        f"[ExecutionEngine] Atomically linked signal {signal_id} to position {position['id']} and marked as executed"
                    )

                # Transaction commits here - all or nothing

            # Return as position dict format expected by position_tracker
            return {
                'id': position['id'],
                'symbol': position['symbol'],
                'side': position['side'],
                'size': position['size'],
                'quantity': position['size'],
                'entry_price': position['entry_price'],
                'entry_time': position['opened_at'],
                'entry_order_id': trade['order_id'],
                'stop_loss_price': trade['stop_loss_price'],
                'stop_loss_order_id': trade['stop_loss_order_id'],
                'status': position['status'],
                'dry_run': is_dry_run,
                'signal_id': signal_id,
                'initial_stop_price': initial_stop_price,
            }

    async def _check_cooldown(self, symbol: str, is_dry_run: bool) -> bool:
        """True if a new entry on `symbol` is allowed by the user's optional `cooldown_hours`
        (unset or 0 = no cooldown). The last exit is this deployment's latest close of the symbol
        in the current mode: from the database (survives restarts) or from a close placed moments
        ago that is not finalised yet."""
        settings = await self._get_execution_settings()
        cooldown_hours = settings.get('cooldown_hours')
        if not cooldown_hours or cooldown_hours <= 0:
            return True
        last = self._last_exit_time.get((symbol, is_dry_run))
        async with self.db.acquire() as conn:
            closed_at = await conn.fetchval(
                "SELECT closed_at FROM positions WHERE symbol = $1 AND status = 'CLOSED' "
                "AND coalesce(deployment_id, -1) = coalesce($2, -1) AND dry_run = $3 "
                "ORDER BY closed_at DESC LIMIT 1",
                symbol, self.deployment_id, is_dry_run,
            )
        if closed_at is not None and (last is None or closed_at > last):
            last = closed_at
        if last is None:
            return True
        return (datetime.now(UTC) - last).total_seconds() >= float(cooldown_hours) * 3600

    async def repair_drift(self) -> dict[str, list]:
        """
        Find live positions whose recorded size differs from the venue's (changed on the venue: a
        manual reduce or add, an order the relay could not record) and repair the record.

        What the venue should hold is the record less the take-profit fills not recorded yet (a
        level partly filled and still resting, or filled since the last stop-rules pass): those
        are the ladder's to record, so they are never counted as drift.

        A difference is only repaired when a later pass finds the SAME difference (the record, the
        venue and the pending fills unchanged), at least DRIFT_MIN_AGE_RULE_PASSES stop-rules
        intervals after it was first seen: an order of the relay's own still in flight is never
        mistaken for drift. The first sighting is an alert (position.drift_detected); the repair
        another (position.drift_repaired):
        - the venue holds LESS: the difference is recorded as a partial close with no price
          ('reduced_on_venue'); its PnL is filled in from the venue's closed-PnL records of the
          orders no record accounts for (pnl_reconciler.reconcile_partial_pnl)
        - the venue holds MORE: the entry price becomes the venue's average for the position (it
          is this record's alone), with the difference recorded as an entry
        A venue position other records share is never repaired (whose part changed cannot be
        told): position.drift_unrepairable, once per difference. A position the venue no longer
        holds at all is the position sync's (a close), not drift. Paper positions never drift.
        One position that cannot be checked does not stop the others.

        Returns {'detected', 'repaired', 'unrepairable'} (position ids).
        """
        out: dict[str, list] = {'detected': [], 'repaired': [], 'unrepairable': []}
        rows = [p for p in await self._own_open_positions(all_modes=True)
                if not p.get('dry_run') and p.get('status') == 'OPEN' and not self._close_owned(p['id'])]
        if not rows:
            self._drift_seen, self._drift_unrepairable = {}, {}
            return out
        try:
            venue_rows = await self.bybit.get_positions(category='linear', settle_coin='USDT')
        except Exception as e:
            logger.warning(f"[Drift] venue positions unreadable ({e}) - checked next pass")
            return out
        venue: dict[tuple[str, str], list[float]] = {}
        for v in venue_rows or []:
            size = float(v.get('size') or 0)
            if size > 0:
                held = venue.setdefault((v.get('symbol'), v.get('side')), [0.0, 0.0])
                held[0] += size
                held[1] = float(v.get('avgPrice') or 0)
        min_age = DRIFT_MIN_AGE_RULE_PASSES * await self._loop_interval('stop_rules_interval_s', 10)
        seen: dict[int, tuple[tuple[float, float, float], float]] = {}
        unrepairable: dict[int, float] = {}
        for p in rows:
            try:
                await self._check_drift(p, venue, min_age, seen, unrepairable, out)
            except Exception as e:
                logger.error(f"[Drift] {p.get('symbol')} #{p.get('id')}: not checked ({e}) - next pass")
                if p['id'] in self._drift_seen:
                    seen[p['id']] = self._drift_seen[p['id']]
                if p['id'] in self._drift_unrepairable:
                    unrepairable[p['id']] = self._drift_unrepairable[p['id']]
        self._drift_seen, self._drift_unrepairable = seen, unrepairable
        return out

    async def _check_drift(self, p: dict[str, Any], venue: dict, min_age: float, seen: dict,
                           unrepairable: dict, out: dict[str, list]) -> None:
        want = 'Buy' if str(p['side']).upper() == 'LONG' else 'Sell'
        venue_size, venue_avg = venue.get((p['symbol'], want), (0.0, 0.0))
        size = float(p['size'])
        if venue_size <= 0:
            return
        pending = await self._pending_ladder_fills(p)
        if pending is None:                       # a ladder order unreadable: cannot tell
            if p['id'] in self._drift_seen:
                seen[p['id']] = self._drift_seen[p['id']]
            return
        expected = size - pending
        if abs(venue_size - expected) <= max(1e-12, size * 1e-6):
            return
        facts = {'symbol': p['symbol'], 'side': p['side'].lower(), 'recorded': size, 'venue': venue_size}
        if pending:
            facts['take_profit_fills_pending'] = pending
        if await self._shares_venue_position(p):
            unrepairable[p['id']] = venue_size
            if self._drift_unrepairable.get(p['id']) != venue_size:
                out['unrepairable'].append(p['id'])
                await self._event('position.drift_unrepairable', 'critical',
                                  f"{p['symbol']} {p['side'].lower()}: the record says {size}, the venue {venue_size}, "
                                  f"and other records share this venue position - not repaired; check it",
                                  facts, p['id'])
            return
        key = (size, venue_size, pending)
        before = self._drift_seen.get(p['id'])
        if before is None or before[0] != key:
            seen[p['id']] = (key, self._clock())
            out['detected'].append(p['id'])
            await self._event('position.drift_detected', 'warning',
                              f"{p['symbol']} {p['side'].lower()}: the record says {size}, the venue {venue_size}"
                              + (f" ({pending:g} more to come from take-profit fills)" if pending else "")
                              + " - repaired on a later sync if it stays so", facts, p['id'])
            return
        seen[p['id']] = before
        if self._clock() - before[1] < min_age:
            return                                 # the same difference, but not old enough yet
        repaired = await self._repair_one(p, size, venue_size, venue_avg, pending)
        if repaired:
            out['repaired'].append(p['id'])
            seen.pop(p['id'], None)
            await self._event('position.drift_repaired', 'warning',
                              f"{p['symbol']} {p['side'].lower()}: record repaired to match the venue's "
                              f"{venue_size} ({repaired})", {**facts, 'repair': repaired}, p['id'])

    async def _pending_ladder_fills(self, row: dict[str, Any]) -> float | None:
        """The quantity the venue has filled on this position's open take-profit levels that is
        not recorded yet (0 without a ladder), or None when a level's order cannot be read."""
        levels = (_json_obj(row.get('management')).get('take_profits') or {}).get('levels') or []
        pending = 0.0
        for lv in levels:
            if lv.get('status') != 'open' or not lv.get('order_id'):
                continue
            fill = await self._order_fill(row['symbol'], lv['order_id'])
            if fill is None:
                return None
            pending += fill[1]
        return pending

    async def _shares_venue_position(self, p: dict[str, Any]) -> bool:
        scope_sql, scope_args = self._account_scope_sql(4)
        async with self.db.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT count(*) FROM positions WHERE symbol = $1 AND side = $2 AND id <> $3 "
                "AND status IN ('OPEN', 'CLOSING')" + scope_sql, p['symbol'], p['side'], p['id'], *scope_args))

    async def _repair_one(self, p: dict[str, Any], size: float, venue_size: float, venue_avg: float,
                          pending: float = 0.0) -> str | None:
        """The repair, under the position's lock, if the record, the venue and the pending ladder
        fills are still what the passes saw. Returns what was done, or None (changed meanwhile:
        next pass)."""
        async with self._position_lock(p['id']):
            row = await self._fresh_row(p['id'])
            if row is None or row['status'] != 'OPEN' or self._close_owned(p['id']) or float(row['size']) != size:
                return None
            now_size = await self._venue_open_size(row['symbol'], row['side'], self.bybit)
            if now_size is None or abs(now_size - venue_size) > max(1e-12, venue_size * 1e-6):
                return None
            now_pending = await self._pending_ladder_fills(row)
            if now_pending is None or abs(now_pending - pending) > 1e-12:
                return None
            expected = size - pending
            if venue_size < expected:
                qty = round(expected - venue_size, 12)
                async with self.db.acquire() as conn:
                    recorded, _ = await self._record_partial_tx(conn, row, qty, None, None, 'reduced_on_venue', False)
                return f'{qty:g} recorded as closed on the venue (PnL from the venue records)' if recorded else None
            qty = round(venue_size - expected, 12)
            new_size = size + qty                  # the pending ladder fills still to be recorded
            entry = float(row['entry_price'])
            new_entry = venue_avg if venue_avg > 0 else entry
            add_price = (new_entry * new_size - entry * size) / qty if venue_avg > 0 else None
            meta = _json_obj(row.get('entry_meta'))
            if venue_avg > 0:
                meta['entry_price_source'] = 'venue_position'
            else:
                meta['entry_price_source'] = 'expected_unconfirmed'
            now = datetime.now(UTC)
            async with self.db.acquire() as conn, conn.transaction():
                updated = await conn.fetchval(
                    "UPDATE positions SET size = $1, entry_price = $2, entry_meta = $3, updated_at = $4 "
                    "WHERE id = $5 AND status = 'OPEN' RETURNING id",
                    new_size, new_entry, json.dumps(meta), now, row['id'])
                if updated is None:
                    return None
                await conn.execute(
                    "INSERT INTO trades (position_id, symbol, trade_type, side, qty, price, time, order_id, "
                    "exit_reason, metadata, created_at, updated_at) "
                    "VALUES ($1, $2, 'entry', $3, $4, $5, $6, NULL, NULL, $7, $6, $6)",
                    row['id'], row['symbol'], 'BUY' if str(row['side']).upper() == 'LONG' else 'SELL', qty,
                    add_price, now, json.dumps({'repair': 'added_on_venue'}))
            return f'{qty:g} added on the venue; entry now the venue average {new_entry:g}'

    async def _event(self, kind: str, severity: str, message: str, data: dict | None = None,
                     position_id: int | None = None) -> None:
        if self.events is not None:
            await self.events.record(kind, severity, message, data, position_id)
        else:
            getattr(logger, severity if severity != 'info' else 'info')(f"[Event] {kind}: {message}")

    async def _position_sync_loop(self) -> None:
        """
        Background task to sync positions with exchange (BACKUP mechanism).

        Primary position sync is real-time via the private WebSocket
        (_on_realtime_position_update). This loop serves as:
        - Backup for missed WebSocket events
        - Reconciliation to catch any discrepancies
        - Import mechanism for positions opened outside the relay

        Interval: the `position_sync_interval_s` setting (an engineering constant, default 60s),
        read each round so a change applies without a restart.
        """
        while self._running:
            ok = True
            if not self.account_readable():
                try:
                    await self._finish_stale_paper_closes()
                    await self._live_rows_without_keys()
                except Exception as e:
                    ok = False
                    logger.opt(exception=True).error(f"[PositionSync] Error: {e!s}")
                await self._beat_and_sleep('position_sync', 'position_sync_interval_s', 60, ok)
                continue
            try:
                # Import any new positions from Bybit
                import_result = await self.position_tracker.import_exchange_positions()
                if import_result.get('imported_count', 0) > 0:
                    logger.info(
                        f"[PositionSync] Imported {import_result['imported_count']} positions from Bybit: "
                        f"{', '.join(import_result.get('imported_symbols', []))}"
                    )

                # Reconcile existing positions (catches any missed WebSocket events)
                await self.position_tracker.sync_all_positions()
                await self._finish_stale_paper_closes()
                # A size that differs from the venue's: alert, and repair on the next pass
                await self.repair_drift()

                # Log sync stats periodically
                if self._realtime_position_updates > 0:
                    logger.debug(
                        f"[PositionSync] Backup sync complete. "
                        f"Real-time updates since start: {self._realtime_position_updates} positions, "
                        f"{self._realtime_order_updates} orders"
                    )

            except Exception as e:
                ok = False
                logger.opt(exception=True).error(f"[PositionSync] Error: {e!s}")
            await self._beat_and_sleep('position_sync', 'position_sync_interval_s', 60, ok)

    async def _stop_audit_loop(self) -> None:
        """Background task: re-arm missing exchange stops every `stop_audit_interval_s` (see audit_stops)."""
        while self._running:
            ok = True
            try:
                await self.audit_stops()
            except Exception as e:
                ok = False
                logger.opt(exception=True).error(f"[StopAudit] Error: {e}")
            await self._beat_and_sleep('stop_audit', 'stop_audit_interval_s', 300, ok)

    async def _stop_rules_loop(self) -> None:
        """Background task: the relay-side stop rules (break-even) every `stop_rules_interval_s`."""
        while self._running:
            ok = True
            try:
                await self.run_stop_rules()
            except Exception as e:
                ok = False
                logger.opt(exception=True).error(f"[StopRules] Error: {e}")
            await self._beat_and_sleep('stop_rules', 'stop_rules_interval_s', 10, ok)

    async def _beat_and_sleep(self, name: str, key: str, default: float, ok: bool = True) -> None:
        """A loop finished a round: note when it last succeeded, and how long it will now wait
        (stuck_loops). A round that failed is no sign of life: one failing every time is stuck too."""
        interval = await self._loop_interval(key, default)
        at = _time.monotonic() if ok else self._beats.get(name, (_time.monotonic(), 0.0))[0]
        self._beats[name] = (at, interval)
        await asyncio.sleep(interval)

    def stuck_loops(self, now: float | None = None) -> list[str]:
        """The background loops that ended, or have not finished a round well for much longer than
        they wait between rounds (a call that hangs, or one that fails every time). Empty while all is well or the engine is stopped."""
        if not self._running:
            return []
        now = _time.monotonic() if now is None else now
        stuck = [name for name, task in zip(self._loop_names, self._tasks, strict=False) if task.done()]
        for name, (at, interval) in self._beats.items():
            if name not in stuck and now - at > 2 * interval + LOOP_STUCK_GRACE_S:
                stuck.append(name)
        return sorted(stuck)

    async def _loop_interval(self, key: str, default: float) -> float:
        """A background loop's interval from the settings (engineering constant, not a trading value)."""
        try:
            value = (await self._get_execution_settings()).get(key)
        except Exception:
            value = None
        return float(value) if value and float(value) > 0 else default

    async def audit_stops(self, prop_rules=None) -> dict[str, Any]:
        """
        Audit stop-loss coverage on every position of this account and re-arm naked ones.

        A naked position (no stop on the exchange) gets, in order of preference: the stop the relay
        recorded for it (its current stop, positions.stop_price, else the one it opened with), or the risk-capped stop from `prop_rules`
        (relay.safety.prop_rules.PropRules) when the account has prop rules. Otherwise it is only
        reported. The stop is tick-aligned so thin coins aren't rejected, and validated against the
        current mark price. In dry-run nothing is sent (StopLossManager honours the flag).

        Returns {'checked', 'naked', 'rearmed', 'would_rearm', 'still_naked': [symbols],
        'breached': [symbols]} ('would_rearm' counts stops a dry-run deployment would have sent;
        'breached' are naked positions whose stop level the price has already passed: reported,
        never replaced by an invented stop).
        """
        from relay.safety.prop_rules import required_native_stop

        out = {'checked': 0, 'naked': 0, 'rearmed': 0, 'would_rearm': 0, 'still_naked': [], 'breached': []}
        is_dry_run = await self.get_dry_run_mode()
        positions = await self.bybit.get_positions(category='linear', settle_coin='USDT')

        # Find positions without stop-loss
        missing_sl = []
        for pos in positions:
            size = float(pos.get('size', 0) or 0)
            if size == 0:
                continue
            out['checked'] += 1
            raw_sl = pos.get('stopLoss', '')
            try:
                has_sl = float(raw_sl or 0) > 0
            except (TypeError, ValueError):
                has_sl = False
            if not has_sl:
                missing_sl.append(pos)
        out['naked'] = len(missing_sl)
        if not missing_sl:
            self._naked_reported = set()
            return out

        logger.error(
            f"[StopAudit] ALERT: {len(missing_sl)} positions WITHOUT stop-loss: "
            f"{', '.join(str(p.get('symbol')) for p in missing_sl)}"
        )

        # The stop the relay recorded (the current one), from THIS account's live rows only. If two
        # deployments on the account hold the same symbol and side (one netted venue position),
        # the more protective of their stops is used.
        recorded: dict[tuple, float] = {}
        scope_sql, scope_args = self._account_scope_sql(1)
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT symbol, side, coalesce(stop_price, initial_stop_price) AS stop FROM positions "
                "WHERE status IN ('OPEN', 'CLOSING') "
                "AND coalesce(stop_price, initial_stop_price) IS NOT NULL" + scope_sql,
                *scope_args,
            )
        for r in rows:
            key, stop = (r['symbol'], r['side']), float(r['stop'])
            if key in recorded:
                stop = max(stop, recorded[key]) if r['side'] == 'LONG' else min(stop, recorded[key])
            recorded[key] = stop

        for pos in missing_sl:
            symbol = pos.get('symbol')
            side = 'long' if pos.get('side') == 'Buy' else 'short'
            size = abs(float(pos.get('size', 0) or 0))
            entry = float(pos.get('avgPrice', 0) or 0)
            mark = float(pos.get('markPrice', 0) or 0) or entry
            stop = recorded.get((symbol, side.upper()))
            from_record = stop is not None          # a live row's own stop: re-armed in any mode
            if stop is None and prop_rules is not None and entry > 0:
                try:
                    stop = required_native_stop(entry, side, entry * size, prop_rules)['stop_price']
                except ValueError:
                    stop = None
            if stop is None or mark <= 0:
                out['still_naked'].append(symbol)
                continue
            try:
                specs = await self._instrument_specs(symbol)
                tick = specs['tick_size'] if specs else None
                final = round_stop_to_tick(stop, tick, side)
                # The venue's entry when it gives one; else (a spot holding whose cost is not in its
                # fills) the mark: the recorded stop only has to be on the right side of the price.
                checked = self.stop_loss_manager.validate_stop(side, entry if entry > 0 else mark, final,
                                                               current_price=mark)
                if not checked['valid']:
                    # The price already passed the stop level: setting it would fire at once and
                    # inventing another one is not ours to do. Report loudly.
                    out['breached'].append(symbol)
                    out['still_naked'].append(symbol)
                    logger.critical(f"[StopAudit] {symbol} {side}: NO STOP and the price {mark} has already "
                                    f"passed its stop level {final} - needs a decision (close or new stop)")
                    continue
                # The venue's positions are live whatever the deployment's mode now (put on paper to
                # "pause" it, say): a live row's own stop is put back. Only an invented one waits.
                if is_dry_run and not from_record:
                    out['would_rearm'] += 1
                    logger.warning(f"[StopAudit] [DRY-RUN] would re-arm stop for {symbol} @ {final}")
                    continue
                res = await self.stop_loss_manager.place_stop_loss(symbol, side, size, final, simulate=False)
                if res.get('success'):
                    out['rearmed'] += 1
                    logger.warning(f"[StopAudit] re-armed stop for {symbol} @ {final}")
                else:
                    out['still_naked'].append(symbol)
            except Exception as e:
                out['still_naked'].append(symbol)
                logger.error(f"[StopAudit] stop re-arm failed for {symbol}: {e}")

        await self._report_naked(out['still_naked'], out['breached'])
        return out

    async def _report_naked(self, naked: list[str], breached: list[str]) -> None:
        """A position left without a stop is a critical event (once until it has one again), so the
        sinks and the console say it, not only the log."""
        now = set(naked)
        for symbol in sorted(now - self._naked_reported):
            why = ("the price has already passed its stop level: close it or set a new stop"
                   if symbol in breached else "no stop is recorded for it and none could be set: set one or close it")
            await self._event('position.naked', 'critical', f"{symbol} is open on the venue WITHOUT a stop - {why}",
                              {'symbol': symbol, 'breached': symbol in breached})
        self._naked_reported = now

    async def _own_open_positions(self, all_modes: bool = False) -> list[dict[str, Any]]:
        """This deployment's open (incl. CLOSING) positions: in its current mode, or with
        all_modes=True paper and live alike (closes follow each position's own mode)."""
        is_dry_run = await self.get_dry_run_mode()
        return [
            p for p in await self.position_tracker.get_all_positions()
            if p.get('deployment_id') == self.deployment_id
            and (all_modes or bool(p.get('dry_run')) == is_dry_run)
        ]

    async def _finish_stale_paper_closes(self, older_than_s: float = 60.0) -> None:
        """A paper close whose bookkeeping failed would stay CLOSING forever (the position sync only
        handles live rows): finish it from the current price."""
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, symbol FROM positions WHERE status = 'CLOSING' AND dry_run "
                "AND coalesce(deployment_id, -1) = coalesce($1, -1) AND updated_at < $2",
                self.deployment_id, cutoff)
        # Taken over like any close (finalize releases what it was handed).
        stale = [{'success': True, 'position_id': r['id'], 'symbol': r['symbol'],
                  'close_order_id': f"dry_run_close_{r['symbol']}", 'reason': 'close_finished_late'}
                 for r in rows if self._own_close(r['id'])]
        if stale:
            logger.warning(f"[PositionSync] finishing {len(stale)} paper close(s) left CLOSING")
            await self.finalize_closed_positions(stale)

    async def close_all(self, reason: str = 'close_all', stop_engine: bool = False) -> dict[str, Any]:
        """
        Close every open position of this deployment, paper and live alike (each close follows its
        position's own mode, so paper ones are only simulated).

        reason='close_all': all reduce-only closes are sent concurrently, then the fills and PnL are
        recorded in one batch (finalize_closed_positions). reason='emergency_stop': each position is
        closed fully (execute_exit); with stop_engine=True new entries are halted FIRST, entries
        already in flight are waited for (so their positions are included), and the engine stops
        even if a close raises.

        Reduce-only closes pass an open circuit breaker (relay.util.circuit_breaker), so a failing
        venue never stops the relay from trying to flatten.
        """
        emergency = reason == 'emergency_stop'
        if stop_engine:
            self.halted_reason = reason      # no new entry from here on
            await self._save_halt(reason)    # ... and after a restart too, until the owner resumes
            await self._event('relay.halted', 'critical',
                              f"{reason}: new entries stopped, closing every position of the deployment",
                              {'reason': reason})
        results: dict[str, Any] = {"closed": 0, "errors": [], "finalized": []}
        retries = 2 if emergency else 1

        async def close_one(position: dict, semaphore: asyncio.Semaphore) -> dict:
            symbol = position.get('symbol', 'UNKNOWN')
            wait_s = 10.0 if emergency else 30.0
            last_error = None
            for attempt in range(retries + 1):
                last_error = None
                busy = False
                async with semaphore:              # only the close itself takes a slot
                    try:
                        if emergency:
                            status = await self.execute_exit(position, reason=reason)
                            if status != 'in_progress':
                                return {"symbol": symbol, "success": True}
                            busy = True
                        else:
                            result = await self.execute_exit_fast(position, reason=reason)
                            if result['success'] and not result.get('in_progress'):
                                return result
                            busy = bool(result.get('in_progress'))
                            if not result['success']:
                                last_error = result.get('error', 'Unknown error')
                    except Exception as e:
                        last_error = str(e)
                if busy and not await self._wait_close_released(position['id'], timeout=wait_s):
                    last_error = "still being closed by another request"
                if attempt < retries:
                    await asyncio.sleep(0.5 if emergency else 0.2)
            # Before reporting a failure, look again: another close may have finished it meanwhile.
            row = await self._fresh_row(position['id'])
            if row is None or row['status'] == 'CLOSED':
                return {"symbol": symbol, "success": True, "already_closed": True}
            return {"symbol": symbol, "success": False,
                    "error": last_error or "still being closed by another request"}

        try:
            if stop_engine:
                still_running = await self._wait_for_entries_in_flight()
                if still_running:
                    results["errors"].append({"symbol": None, "error": f"{still_running} entr(ies) still running "
                                              f"after the wait - check for positions opened after the stop"})
            positions = await self._own_open_positions(all_modes=True)
            if positions:
                logger.info(f"[CloseAll] Closing {len(positions)} positions (reason: {reason})")
                semaphore = asyncio.Semaphore(5 if emergency else 10)
                outcomes = await asyncio.gather(*[close_one(p, semaphore) for p in positions],
                                                return_exceptions=True)
                successful = []
                for position, outcome in zip(positions, outcomes):
                    if isinstance(outcome, Exception):
                        results["errors"].append({"symbol": position.get('symbol'), "error": str(outcome)})
                    elif outcome.get("success"):
                        results["closed"] += 1
                        successful.append(outcome)
                    else:
                        results["errors"].append({"symbol": outcome.get("symbol"), "error": outcome.get("error")})
                if successful and not emergency:
                    try:
                        results["finalized"] = await self.finalize_closed_positions(successful)
                    except Exception as e:
                        logger.error(f"[CloseAll] PnL finalization failed: {e}")
                    problems = {
                        "error": lambda f: f"recording the close failed: {f['error']}",
                        "partial": lambda f: f"only {f['closed_qty']} closed; {f['remaining_qty']} still open",
                        "nothing_filled": lambda f: f"the close filled nothing; {f['remaining_qty']} still open",
                        "unconfirmed": lambda f: "close sent but its fill is not confirmed yet (the sync settles it)",
                        "no_price": lambda f: "paper close waiting for a price to simulate the fill",
                    }
                    for f in results["finalized"]:
                        for flag, text in problems.items():
                            if f.get(flag):
                                results["closed"] -= 1
                                results["errors"].append({"symbol": f["symbol"], "error": text(f)})
                try:
                    await self.position_tracker.sync_all_positions()
                except Exception as e:
                    logger.error(f"[CloseAll] post-close sync failed: {e}")
        finally:
            if stop_engine:
                await self.stop()

        return {
            "success": len(results["errors"]) == 0,
            "positions_closed": results["closed"],
            "errors": results["errors"],
            "finalized": results["finalized"],
        }

    async def _wait_close_released(self, position_id: int, timeout: float = 30.0) -> bool:
        """Wait (bounded) until no other close owns this position. True if released."""
        deadline = _time.monotonic() + timeout
        while self._close_owned(position_id) and _time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        return not self._close_owned(position_id)

    async def _wait_for_entries_in_flight(self, timeout: float = 30.0) -> int:
        """Wait (bounded) until entries that passed the halt check before it was set have finished,
        so the positions they open are part of the emergency close. Returns how many are still
        running after the wait."""
        pending = [f for f in self._entries_in_flight if not f.done()]
        if not pending:
            return 0
        logger.warning(f"[CloseAll] waiting for {len(pending)} entr(ies) in flight before closing")
        _, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            logger.critical(f"[CloseAll] {len(still)} entr(ies) still running after {timeout}s - "
                            f"check for positions opened after the emergency stop")
        return len(still)
