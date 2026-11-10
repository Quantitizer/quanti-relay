"""
Risk Manager - pre-trade risk checks.

Every limit is the user's own optional setting; an unset limit is not checked (the relay ships no
trading numbers). Checks, in order:
- account equity readable (and >= the optional min_balance)
- position count (max_positions) and open exposure (max_exposure_pct) before the order
- one position per symbol within the risk scope (pyramiding / reversal are not supported)
- today's realised loss (daily_loss_limit_pct)
and, once the order's final size is known, `check_new_exposure` counts the new order too.

Scope. Paper trades are limited by this deployment's paper positions. Live trades are limited by
every live position on the ACCOUNT (other deployments and positions opened outside the relay
included): that is the exposure the venue actually carries. The engine passes the scope as a
WHERE-fragment builder; without one, the mode alone scopes the queries.

A check that cannot be evaluated (database or venue error) REFUSES the trade: a limit the user set
must never be silently skipped.
"""
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from relay.execution.dry_run import DryRunFlag, resolve_dry_run

ScopeFn = Callable[[int, bool], tuple[str, list]]


def _mode_only_scope(start: int, is_dry_run: bool) -> tuple[str, list]:
    return f" AND dry_run = ${start}", [is_dry_run]


def _refuse(reason: str) -> dict[str, Any]:
    return {'approved': False, 'reason': reason, 'max_size': 0.0}


class RiskManager:
    def __init__(
        self,
        db_pool,
        bybit_client,
        settings_manager,
        min_balance: float | None = None,
        dry_run: DryRunFlag = False,
        scope: ScopeFn | None = None,
    ):
        """
        Args:
            db_pool: Database (relay.db.Database)
            bybit_client: venue client with `get_balance_safe(coin) -> (equity, source)`
            settings_manager: provides `async get_execution_settings() -> dict` (max_positions,
                max_exposure_pct, daily_loss_limit_pct; each None = off)
            min_balance: refuse new trades below this account equity (USDT); None = off
            dry_run: the engine's mode flag (bool or callable), read per check
            scope: (start_index, is_dry_run) -> (" AND ...", params) limiting the position queries
                to the risk scope (see the module doc); default: the mode only
        """
        self.db = db_pool
        self.bybit = bybit_client
        self.settings = settings_manager
        self.min_balance = min_balance
        self._dry_run = dry_run
        self._scope = scope or _mode_only_scope

    async def _mode(self, is_dry_run: bool | None) -> bool:
        """The mode the checks count in: the entry's own (a paper entry on a live deployment is
        checked against the paper book), else the engine's."""
        return is_dry_run if is_dry_run is not None else await resolve_dry_run(self._dry_run)

    async def _scoped(self, start: int, is_dry_run: bool | None = None) -> tuple[str, list]:
        return self._scope(start, await self._mode(is_dry_run))

    async def check_trade_approval(self, symbol: str, signal: dict[str, Any],
                                   is_dry_run: bool | None = None) -> dict[str, Any]:
        """All pre-order checks, counted in the entry's mode `is_dry_run` (None: the engine's).
        Returns {'approved', 'reason', 'max_size'}; max_size is the account equity (the sizer's
        base for equity_pct / risk_pct)."""
        balance_check = await self._check_account_balance()
        if not balance_check['approved']:
            return balance_check

        limit_check = await self._check_position_limits(balance_check['max_size'], is_dry_run)
        if not limit_check['approved']:
            return limit_check

        conflict_check = await self._check_symbol_conflicts(symbol, is_dry_run)
        if not conflict_check['approved']:
            return conflict_check

        loss_check = await self._check_daily_loss_limit(balance_check['max_size'], is_dry_run)
        if not loss_check['approved']:
            return loss_check

        return {'approved': True, 'reason': 'All risk checks passed', 'max_size': balance_check['max_size']}

    async def _check_account_balance(self) -> dict[str, Any]:
        """Account equity must be readable and positive, and >= the optional min_balance."""
        try:
            equity, source = await self.bybit.get_balance_safe(coin="USDT")
        except Exception as e:
            logger.error(f"[RiskManager] Error reading account equity: {e}")
            return _refuse(f'Balance check failed: {e}')
        if equity > 0:
            if self.min_balance is not None and equity < self.min_balance:
                return _refuse(f'Insufficient balance: ${equity:.2f} < ${self.min_balance:.2f}')
            return {'approved': True, 'reason': 'Balance sufficient', 'max_size': equity}
        if source == 'wallet':
            return _refuse('Account equity is 0')
        logger.warning(f"[RiskManager] Unable to retrieve account equity (source: {source})")
        return _refuse(f'Unable to retrieve account balance (source: {source})')

    async def _open_exposure(self, is_dry_run: bool | None = None) -> tuple[int, float]:
        """(open position count, open notional) within the risk scope."""
        scope_sql, scope_args = await self._scoped(1, is_dry_run)
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n, COALESCE(SUM(entry_price * size), 0) AS notional FROM positions "
                "WHERE status IN ('OPEN', 'CLOSING')" + scope_sql,
                *scope_args,
            )
        return int(row['n']), float(row['notional'])

    async def _check_position_limits(self, equity: float, is_dry_run: bool | None = None) -> dict[str, Any]:
        try:
            settings = await self.settings.get_execution_settings()
            max_positions = settings.get('max_positions')
            max_exposure_pct = settings.get('max_exposure_pct')
            if max_positions is None and max_exposure_pct is None:
                return {'approved': True, 'reason': 'No position limits configured', 'max_size': 0.0}
            open_count, open_notional = await self._open_exposure(is_dry_run)
            if max_positions is not None and open_count >= max_positions:
                return _refuse(f'Position limit reached: {open_count}/{max_positions}')
            if max_exposure_pct is not None:
                exposure_pct = open_notional / equity * 100 if equity > 0 else float('inf')
                if exposure_pct >= max_exposure_pct:
                    return _refuse(f'Exposure limit reached: {exposure_pct:.1f}% >= {max_exposure_pct:.1f}%')
            return {'approved': True, 'reason': 'Position limits OK', 'max_size': 0.0}
        except Exception as e:
            logger.error(f"[RiskManager] Error checking position limits: {e}")
            return _refuse(f'Position limit check failed: {e}')

    async def check_new_exposure(self, notional: float, is_dry_run: bool | None = None) -> dict[str, Any]:
        """Once the order's size is final: would open exposure PLUS this order exceed the user's
        max_exposure_pct? (Unset = not checked.)"""
        try:
            max_exposure_pct = (await self.settings.get_execution_settings()).get('max_exposure_pct')
            if max_exposure_pct is None:
                return {'approved': True, 'reason': 'No exposure limit configured', 'max_size': 0.0}
            equity, _ = await self.bybit.get_balance_safe(coin="USDT")
            if not equity or equity <= 0:
                return _refuse('Exposure check failed: account equity unavailable')
            _, open_notional = await self._open_exposure(is_dry_run)
            after_pct = (open_notional + notional) / equity * 100
            if after_pct > max_exposure_pct + 1e-9:
                return _refuse(f'Exposure limit: this order would bring exposure to {after_pct:.1f}% '
                               f'> {max_exposure_pct:.1f}%')
            return {'approved': True, 'reason': f'Exposure after order {after_pct:.1f}%', 'max_size': 0.0}
        except Exception as e:
            logger.error(f"[RiskManager] Error checking new exposure: {e}")
            return _refuse(f'Exposure check failed: {e}')

    async def _check_symbol_conflicts(self, symbol: str, is_dry_run: bool | None = None) -> dict[str, Any]:
        """One position per symbol within the risk scope. Live positions on one account net on the
        venue (one-way mode): a second deployment's order would merge into the first's position."""
        try:
            scope_sql, scope_args = await self._scoped(2, is_dry_run)
            async with self.db.acquire() as conn:
                n = await conn.fetchval(
                    "SELECT COUNT(*) FROM positions WHERE symbol = $1 AND status IN ('OPEN', 'CLOSING')"
                    + scope_sql,
                    symbol, *scope_args,
                )
            if n:
                return _refuse(f'Symbol {symbol} already has an open position')
            return {'approved': True, 'reason': 'No symbol conflict', 'max_size': 0.0}
        except Exception as e:
            logger.error(f"[RiskManager] Error checking symbol conflicts: {e}")
            return _refuse(f'Symbol conflict check failed: {e}')

    async def _check_daily_loss_limit(self, equity: float, is_dry_run: bool | None = None) -> dict[str, Any]:
        """Today's (UTC) realised loss within the scope vs the user's daily_loss_limit_pct of equity.

        Each loss counts on the day it was REALISED: a partial close today counts today, and a
        position closed today contributes its result minus the partial closes recorded for it (those
        count on their own days). Counted: the venue's verified PnL and the relay's fill-based PnL
        (live), the simulated PnL (paper). A close today whose PnL is not known yet makes the day's
        loss unknown: the check then REFUSES (naming the closes) rather than treat it as zero."""
        try:
            daily_loss_limit_pct = (await self.settings.get_execution_settings()).get('daily_loss_limit_pct')
            if daily_loss_limit_pct is None:
                return {'approved': True, 'reason': 'Daily loss limit not set', 'max_size': 0.0}
            today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            is_dry_run = await self._mode(is_dry_run)
            scope_sql, scope_args = self._scope(3, is_dry_run)
            async with self.db.acquire() as conn:
                closed = await conn.fetch(
                    "SELECT id, symbol, pnl, pnl_source FROM positions WHERE status = 'CLOSED' "
                    "AND closed_at >= $1 AND dry_run = $2" + scope_sql,
                    today_start, is_dry_run, *scope_args,
                )
                partial_rows = await conn.fetch(
                    "SELECT t.position_id, t.time, t.metadata FROM trades t JOIN positions p ON p.id = t.position_id "
                    "WHERE t.trade_type = 'partial_exit' AND (t.time >= $1 OR p.closed_at >= $1) "
                    "AND p.dry_run = $2" + scope_sql.replace(" dry_run", " p.dry_run").replace(
                        " deployment_id", " p.deployment_id"),
                    today_start, is_dry_run, *scope_args,
                )
            closed_ids = {r['id'] for r in closed}
            unknown: list[str] = []
            total = 0.0
            partial_by_position: dict[int, float] = {}
            for r in partial_rows:
                pnl = _partial_pnl(r['metadata'])
                when = r['time']
                today = when is not None and when >= today_start
                if r['position_id'] in closed_ids:
                    # Closed today: its own PnL covers every partial; only partials from EARLIER days
                    # are subtracted (they counted on their own days). Today's partials need no value.
                    if not today:
                        prior = partial_by_position.get(r['position_id'], 0.0)
                        partial_by_position[r['position_id']] = float('nan') if pnl is None else prior + pnl
                    continue
                if today:
                    if pnl is None:
                        unknown.append(f"partial close of position #{r['position_id']}")
                    else:
                        total += pnl
            for r in closed:
                # Unknown, or an old-style estimate (not a fill or venue result): the day is unknown.
                if r['pnl'] is None or r['pnl_source'] not in ('dry_run', 'venue_api', 'local_calc'):
                    unknown.append(f"{r['symbol']} #{r['id']}")
                    continue
                earlier = partial_by_position.get(r['id'], 0.0)
                if math.isnan(earlier):   # a partial with unknown PnL makes the remainder unknown
                    unknown.append(f"{r['symbol']} #{r['id']}")
                    continue
                total += float(r['pnl']) - earlier
            if unknown:
                return _refuse(f"Daily loss unknown: not priced yet: {', '.join(unknown[:5])}"
                               f"{' ...' if len(unknown) > 5 else ''} (the PnL reconciler prices live closes and "
                               f"partial closes from the venue's records)")
            daily_loss_pct = abs(total) / equity * 100 if equity > 0 and total < 0 else 0.0
            if daily_loss_pct >= daily_loss_limit_pct:
                return _refuse(f'Daily loss limit breached: {daily_loss_pct:.1f}% >= {daily_loss_limit_pct:.1f}%')
            return {'approved': True, 'reason': 'Daily loss limit OK', 'max_size': 0.0}
        except Exception as e:
            # FAIL CLOSED: the user set this limit; an unreadable day must not bypass it.
            logger.error(f"[RiskManager] Daily loss check failed - refusing: {e}")
            return _refuse(f'Daily loss check failed: {e}')


def _partial_pnl(metadata) -> float | None:
    if isinstance(metadata, str):
        import json
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    value = (metadata or {}).get('pnl')
    return float(value) if value is not None else None
