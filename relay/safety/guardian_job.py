"""NOT WIRED YET: the running relay does not use this module. It is kept for phase 2 (deployment
files with drawdown limits), and reviewed and fixed when it is connected (tests/test_unwired_modules.py
keeps the running relay from importing it before then).

Account-DD guardian LOOP — the live circuit breaker for an exchange account.

Runs on a sub-60s interval per account: fetch the account MtM equity (balance + unrealized), feed
that account's stateful AccountDDGuardian, and FLATTEN ALL its positions (reduce-only) the instant
equity touches a DD floor (earlier by the optional buffer). A 00:00-UTC job re-arms a daily-DD halt;
a max-DD halt is lifetime (account-ending) and never auto-cleared.

Only accounts whose deployments configure drawdown limits are guarded. Each guardian persists halt /
intraday-peak state on its `accounts.guardian_state` row and is rebuilt only when the DD floors /
balance change.

LIVE vs DRY-RUN. A breach always halts new entries. The flatten (cancel-all + reduce-only closes) is
only REAL when `live=True`, i.e. the account has at least one live deployment: then it must never be
silenced by a dry-run flag. On an account where every deployment is in dry-run, the relay has opened
nothing real, so the flatten is logged and NOT sent — it must never close positions a person opened
by hand on the same account.

Pure flatten logic (`flatten_all`) takes injected position/close callables so it is unit-tested
without a live exchange.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from relay.db.models import Account
from relay.execution.order_executor import format_order_qty
from relay.safety.account_dd_guardian import AccountDDGuardian

logger = logging.getLogger(__name__)

# Per-account guardian registry, keyed by guardian_key(account).
_guardians: dict[str, AccountDDGuardian] = {}


def guardian_key(account) -> str:
    """The guardian-registry key for an Account — the single source of truth for per-account keying.
    The process-wide Bybit account (venue 'bybit' with NO per-account credentials_ref) is "default".
    Every OTHER account — Hyperliquid, or a Bybit account with its own keys (venue 'bybit' WITH a
    credentials_ref) — gets its own key = its unique account name, so two Bybit accounts never share
    a halt state and each is guarded on ITS OWN drawdown."""
    venue = (getattr(account, "venue", None) or "bybit")
    if venue == "bybit" and not getattr(account, "credentials_ref", None):
        return "default"
    return getattr(account, "name", None) or venue


def load_persisted_state(raw: str | None) -> dict:
    """Parse a persisted guardian_state. FAIL CLOSED: if it is non-empty but unreadable, a lifetime
    max-DD halt may be lost in it, and a blown account must NOT silently come back un-halted. Halt
    (max_dd) until an operator inspects + force-resets. An empty/None state is fine (no halt)."""
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError) as e:
        logger.error("[guardian] CORRUPT guardian_state (%s) — failing CLOSED (halting max_dd) until "
                     "inspected: %r", e, (raw or "")[:200])
        return {"halted": True, "halt_type": "max_dd"}


def _get_guardian_account(account_key: str, cfg, persisted_state: dict | None = None,
                          account_id: int | None = None) -> AccountDDGuardian:
    """Build/reuse an account's guardian. Rebuilds when the DD floors / balance change, and a rebuild
    KEEPS the previous guardian's state (a halt must survive a settings change). On a first build it
    loads `persisted_state` (accounts.guardian_state) so a lifetime max-DD halt survives a restart.
    `account_id` is stamped on the guardian so ticks / daily-reset can persist back to the right row."""
    key_t = (cfg.initial_balance, cfg.daily_dd_pct, cfg.max_dd_pct, cfg.dd_buffer_pct)
    old = _guardians.get(account_key)
    if old is None or getattr(old, "_gc_key", None) != key_t:
        g = AccountDDGuardian(cfg)
        g._gc_key = key_t
        if old is not None:
            g.load_state(old.to_state())
        elif persisted_state:
            g.load_state(persisted_state)
        g._account_id = account_id if account_id is not None else getattr(old, "_account_id", None)
        _guardians[account_key] = g
        logger.info("[guardian:%s] armed: initial=%s daily=%s%% max=%s%% buffer=%s%% halt=%s",
                    account_key, cfg.initial_balance, cfg.daily_dd_pct, cfg.max_dd_pct,
                    cfg.dd_buffer_pct, g.halt_type)
        return g
    if account_id is not None:
        old._account_id = account_id      # keep the persistence target fresh across ticks
    return old


async def _save_guardian_state_account(db_handle, guardian: AccountDDGuardian) -> None:
    """Persist an account guardian's state to its accounts row (durable max-DD halt).
    No-op if the guardian wasn't stamped with an account id."""
    account_id = getattr(guardian, "_account_id", None)
    if account_id is None:
        return
    async with db_handle.session() as db:
        account = await db.get(Account, account_id)
        if account is not None:
            account.guardian_state = json.dumps(guardian.to_state())
            await db.commit()


async def ensure_armed_accounts(db_handle, guardian_configs: dict) -> None:
    """Eagerly load EVERY guarded account's PERSISTED guardian state into `_guardians` at startup —
    BEFORE its first tick — so entries_allowed(key) reflects a persisted lifetime max-DD halt from
    tick 0. Without this, a signal in the startup window would see entries_allowed==True on a blown,
    halted account. Build-only: loads state, never ticks/flattens.

    `guardian_configs`: {account name: GuardianConfig} for the accounts whose deployments configure
    drawdown limits."""
    from sqlalchemy import select
    async with db_handle.session() as db:
        accts = (await db.execute(select(Account).where(Account.enabled.is_(True)))).scalars().all()
        for account in accts:
            cfg = guardian_configs.get(account.name)
            if cfg is None:
                continue
            persisted = load_persisted_state(account.guardian_state)
            _get_guardian_account(guardian_key(account), cfg, persisted_state=persisted, account_id=account.id)


def entries_allowed(account_key: str = "default", guardian_configured: bool = True) -> bool:
    """New entries are blocked while THAT account's guardian is halted (max-DD or same-day daily-DD).

    FAIL CLOSED for an account that configures a guardian but has none armed: after
    ensure_armed_accounts every guarded account IS armed (with its persisted halt), so a missing one
    must NOT trade. An account with no drawdown limits configured has no guardian, and is allowed."""
    g = _guardians.get(account_key)
    if g is None:
        return not guardian_configured
    return not g.halted


def _resolve_close_fn(bybit_client, order_executor, live: bool):
    """Prefer OrderExecutor.close_position_fast (reduce-only). If it isn't available (engine not fully
    initialized), fall back to a raw reduce-only market close on the client directly — a DD breach is
    an emergency, so the position must close regardless.

    `live`: the account has a live deployment -> the close is REAL even if a dry-run flag says
    otherwise (simulate=False). Not live -> nothing is placed (see LIVE vs DRY-RUN in the module doc).

    MULTI-ACCOUNT: the flatten close is ALWAYS routed to `bybit_client` — the account's own client or
    adapter. `order_executor.close_position_fast` defaults to the order_executor's OWN (default) client,
    so a keyed account's breach would otherwise flatten on the DEFAULT account and never actually close
    the breached one. Binding `client=bybit_client` makes the breach flatten hit ITS OWN account."""
    if not live:
        async def base(symbol: str, side: str, qty: float):
            logger.warning("[guardian] [DRY-RUN] would close %s %s %s (no live deployment)",
                           side, qty, symbol)
            return {"simulated": True}
    elif order_executor is not None and hasattr(order_executor, "close_position_fast"):
        _base_fn = order_executor.close_position_fast

        async def base(symbol: str, side: str, qty: float):
            # A LIVE account's flatten is real, whatever a dry-run flag says.
            return await _base_fn(symbol, side, qty, client=bybit_client, simulate=False)
    else:
        async def base(symbol: str, side: str, qty: float):
            bybit_side = "Sell" if side == "long" else "Buy"     # close = opposite of position side
            await bybit_client.place_market_order(symbol=symbol, side=bybit_side, qty=format_order_qty(qty),
                                                  category="linear", reduce_only=True)
            return {"success": True}
    # Reduce-only closes pass an open circuit breaker by design (relay.util.circuit_breaker), so an
    # exchange incident cannot block the flatten.
    return base


async def _cancel_all_before_flatten(client, breach_type: str, tag: str, live: bool) -> None:
    """Cancel EVERY resting order on a breached account, BEFORE the flatten.

    `flatten_all` closes POSITIONS and cancels nothing, and every later entry stops at the guardian
    gate. So resting ENTRY orders would survive the flatten and could RE-OPEN positions on a halted,
    already-breached account — until the daily reset at 00:00 UTC.

    ORDER IS THE POINT: flattening first leaves a window in which a resting order fills and re-opens
    a leg the flatten just closed.

    Scope is the WHOLE account, not just the relay's own orders: a breach means this account has
    failed, so every resting order is live risk. Only when `live` (see the module doc).

    Fully swallowed: a cancel failure must NEVER be able to block the emergency flatten."""
    if not live:
        logger.critical("[%s] [DRY-RUN] would cancel ALL resting orders before the flatten (%s)",
                        tag, breach_type)
        return
    try:
        await client.cancel_all_orders(category="linear", settle_coin="USDT")
        logger.critical("[%s] cancelled ALL resting orders before the flatten (%s)", tag, breach_type)
    except Exception as e:
        logger.error("[%s] cancel-all FAILED before the flatten — resting orders may RE-OPEN "
                     "positions on a HALTED account: %s", tag, e)


async def flatten_all(positions: list[dict], close_fn, reason: str) -> dict:
    """Reduce-only close EVERY open position. positions: Bybit v5 rows; close_fn(symbol, side, qty)
    is OrderExecutor.close_position_fast (side = position side 'long'/'short'), which reports a
    failure in its result rather than raising: that result is checked, so a refused close is never
    counted as closed. Returns {'closed', 'failed': [symbols], 'simulated'}."""
    out = {"closed": 0, "failed": [], "simulated": 0}
    for p in positions:
        try:
            size = abs(float(p.get("size", 0) or 0))
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        symbol = p.get("symbol")
        pos_side = "long" if str(p.get("side")) == "Buy" else "short"
        try:
            res = await close_fn(symbol, pos_side, size) or {}
        except Exception as e:
            res = {"success": False, "error": str(e)}
        if res.get("simulated"):
            out["simulated"] += 1
        elif res.get("success"):
            out["closed"] += 1
            logger.critical("[guardian] FLATTEN %s %s %s (%s)", pos_side, size, symbol, reason)
        else:
            out["failed"].append(symbol)
            logger.critical("[guardian] flatten close FAILED for %s %s %s: %s - STILL OPEN",
                            pos_side, size, symbol, res.get("error"))
    return out



async def _safety_read(fn, **kwargs):
    """Call a venue read with the breaker bypass when the client supports it (Bybit), plainly
    otherwise."""
    import inspect
    try:
        if "bypass_breaker" in inspect.signature(fn).parameters:
            kwargs["bypass_breaker"] = True
    except (TypeError, ValueError):
        pass
    return await fn(**kwargs)

async def run_guardian_check_account(db_handle, adapter, guardian_cfg, account_key: str,
                                     order_executor=None, persisted_state: dict | None = None,
                                     account_id: int | None = None, live: bool = False) -> dict:
    """One guardian tick for an account: equity -> observe -> flatten-all on breach, on THIS account's
    adapter + guardian, so it flattens ONLY this account's positions. State is DB-persisted to
    accounts.guardian_state when `account_id` is given (a lifetime max-DD halt survives a restart).
    `live`: whether the flatten is real (see LIVE vs DRY-RUN in the module doc)."""
    guardian = _get_guardian_account(account_key, guardian_cfg, persisted_state, account_id)
    try:
        # Bybit: /v5/account/wallet-balance -> totalEquity (mark-to-market, incl. unrealized). NOT
        # /v5/account/info, which is margin CONFIG and has no equity.
        # A safety read: passes an open circuit breaker (it adds no risk), so the guardian sees the
        # account during exactly the incident it exists for - without resetting the shared breaker.
        equity = float(await _safety_read(adapter.get_account_balance))
    except Exception as e:
        logger.warning("[guardian:%s] equity fetch failed: %s", account_key, e)
        return {"error": "equity_fetch"}
    if equity <= 0:
        logger.warning("[guardian:%s] equity reads %s - skipped this tick", account_key, equity)
        return {"skipped": "no_equity"}
    prev_peak = guardian.intraday_peak
    verdict = guardian.observe(equity, datetime.now(UTC))
    if not verdict.breach:
        # Persist when the intraday peak advances to a new high (so a mid-day restart restores the true
        # trailing daily floor, not a stale lower peak). New highs are infrequent -> few writes.
        if guardian.intraday_peak != prev_peak:
            await _save_guardian_state_account(db_handle, guardian)
        return {"ok": True, "equity": round(equity, 2), "halted": guardian.halted,
                "max_floor": verdict.max_floor, "daily_floor": verdict.daily_floor}
    logger.critical("[guardian:%s] %s BREACH: equity %.2f <= floor (max %s / daily %s) — FLATTEN ALL",
                    account_key, verdict.breach_type, equity, verdict.max_floor, verdict.daily_floor)
    positions_unreadable = False
    try:
        positions = await _safety_read(adapter.get_positions, category="linear", settle_coin="USDT")
    except Exception as e:
        logger.critical("[guardian:%s] cannot read positions during the flatten - NOTHING could be "
                        "closed, check the account now: %s", account_key, e)
        positions, positions_unreadable = [], True
    await _cancel_all_before_flatten(adapter, verdict.breach_type, f"guardian:{account_key}", live)
    close_fn = _resolve_close_fn(adapter, order_executor, live)
    result = await flatten_all(positions, close_fn, f"guardian:{account_key}:{verdict.breach_type}")
    await _save_guardian_state_account(db_handle, guardian)   # persist the halt so it survives a restart
    return {"breach": verdict.breach_type, **result, "equity": round(equity, 2), "live": live,
            "positions_unreadable": positions_unreadable}


async def guardian_daily_reset(db_handle) -> dict:
    """00:00-UTC re-arm: clears a daily-DD halt (max-DD halt is lifetime) and resets the peak, for
    every armed account guardian."""
    if not _guardians:
        return {"skipped": "not_armed"}
    rearmed = {}
    for key, g in _guardians.items():
        rearmed[key] = g.daily_reset()          # clears a daily-DD halt; a max-DD halt is lifetime
        await _save_guardian_state_account(db_handle, g)   # persist the re-arm / reset peak per account
    logger.info("[guardian] 00:00 UTC daily reset (%d account(s), rearmed=%s)", len(_guardians), rearmed)
    return {"rearmed": rearmed}
