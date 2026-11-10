"""NOT WIRED YET: the running relay does not use this module. It is kept for phase 2 (several accounts per relay), and reviewed and fixed when it is connected (tests/test_unwired_modules.py keeps the running relay from importing it before then).

Keyed account book importer — the missing half of the reconciliation loop.

THE GAP THIS CLOSES. On an account with its own keys, `PositionTracker.import_exchange_positions`
never looks (it reads the DEFAULT client) and `sync_all_positions` iterates DB ROWS — it can CLOSE
and UPDATE but structurally cannot CREATE. So a position the executor failed to record would be a
PERMANENT orphan, while a row with no position self-erases within one sync. Every one-off failure
in the entry or exit path would become a permanent unmanaged position: never exited, never
risk-accounted, but real exposure against the account's drawdown limit.

This module makes the loop SYMMETRIC. Run alongside the per-account sync, it ADOPTS exchange
positions that have no OPEN DB row and CORRECTS rows whose size or side disagrees with the exchange —
so a dropped row self-heals on the next tick instead of stranding forever.

TRUST THE EXCHANGE. The exchange is the only authority on what is actually held; the DB is a cache.
Every repair here moves the DB toward the exchange, never the reverse, and this module NEVER places
an order.

OWNERSHIP. When the account has exactly one deployment, attribution is deterministic: any position
on that account belongs to that deployment. When the account resolves to more than one candidate
deployment the position is AMBIGUOUS and is deliberately SKIPPED (counted + logged) rather than
guessed at — a wrong owner is worse than a known gap, because the wrong deployment would then manage
someone else's position.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from relay.db.models import Deployment as StrategyDeployment
from relay.db.models import Position as BotPosition
from relay.execution.book_reconcile import reconcile

logger = logging.getLogger(__name__)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _side(bybit_side: str) -> str:
    return "LONG" if str(bybit_side) == "Buy" else "SHORT"


def _opened_at(raw) -> datetime:
    """Exchange createdTime (ms) -> aware datetime, so an adopted row carries its REAL age rather
    than looking like it was opened at adoption time (which would corrupt hold-time analytics)."""
    try:
        v = int(raw)
        if v > 0:
            return datetime.fromtimestamp(v / 1000, tz=UTC)
    except (TypeError, ValueError):
        pass
    return datetime.now(UTC)


async def _resolve_owner(db, deployment_ids: set[int]) -> int | None:
    """deployment_id if the account resolves to EXACTLY ONE enabled deployment, else None.
    Deliberately strict — see OWNERSHIP in the module docstring."""
    rows = list((await db.execute(select(StrategyDeployment).where(
        StrategyDeployment.id.in_(list(deployment_ids))))).scalars().all())
    enabled = [d for d in rows if d.enabled]
    # Prefer the enabled set; fall back to a lone disabled deployment so a PAUSED account (exactly the
    # state you pause into while repairing) can still self-heal its book.
    candidates = enabled or rows
    if len(candidates) == 1:
        return candidates[0].id
    return None


async def import_keyed_account_positions(db_handle, adapter, deployment_ids: set[int], *,
                                         dry_run: bool = False) -> dict:
    """Adopt/repair the DB book for ONE keyed account against its OWN client.

    `deployment_ids` are the deployments on that account.

    `adapter` MUST be that account's adapter — never the default client. Returns a counts dict;
    on any read failure it returns early having changed NOTHING (fail-closed: a partial view of the
    exchange must never drive a write).
    """
    out = {"exchange": 0, "adopted": 0, "size_fixed": 0, "side_fixed": 0,
           "ambiguous": 0, "skipped_conflict": 0, "errors": 0}
    if not deployment_ids:
        return out

    # ---- 1. the TRUE book, from the account that owns it (abort on failure) ----
    try:
        raw = await adapter.get_positions(category="linear", settle_coin="USDT")
    except Exception as e:
        logger.error("[keyed-import] cannot read the account's positions (%s) — no changes made", e)
        out["errors"] = 1
        return out

    exch: dict[str, dict] = {}
    for p in raw or []:
        size = abs(_f(p.get("size")))
        if size <= 0:
            continue
        sym = str(p.get("symbol") or "")
        if not sym:
            continue
        exch[sym] = {"size": size, "side": _side(p.get("side")),
                     "entry": _f(p.get("avgPrice")), "upnl": _f(p.get("unrealisedPnl")),
                     "opened_at": _opened_at(p.get("createdTime"))}
    out["exchange"] = len(exch)

    async with db_handle.session() as db:
        deployment_id = await _resolve_owner(db, deployment_ids)

        rows = list((await db.execute(select(BotPosition).where(
            BotPosition.status == "OPEN",
            BotPosition.dry_run.is_(False),   # paper positions have no exchange counterpart
            BotPosition.deployment_id.in_(list(deployment_ids)),
        ))).scalars().all())
        by_symbol = {r.symbol: r for r in rows}
        db_book = {r.symbol: {"size": _f(r.size), "side": str(r.side).upper()} for r in rows}

        # ---- 2. pure diff (book_reconcile) ----
        res = reconcile(db_book, {s: {"size": v["size"], "side": v["side"]} for s, v in exch.items()})
        if not res.safe_to_rebalance:
            # Informational here — this module only ever moves the DB toward the exchange, so it is
            # safe to repair even large drift. The flag matters to the REBALANCE, which must not trade
            # on a wrong picture; surface it loudly so that gate can act on it.
            logger.critical("[keyed-import] LARGE BOOK DRIFT on deployments %s: %s", sorted(deployment_ids), res.reason)

        now = datetime.now(UTC)

        # ---- 3. ADOPT positions the DB has never heard of (the orphan cure) ----
        for sym in res.untracked_exch:
            info = exch[sym]
            if deployment_id is None:
                out["ambiguous"] += 1
                logger.error("[keyed-import] %s: on the exchange with no DB row, but the account maps "
                             "to 0 or >1 deployments — SKIPPING rather than guessing an owner", sym)
                continue
            # Respect the partial unique index (coalesce(deployment_id,-1), symbol, side) WHERE status='OPEN'.
            clash = (await db.execute(select(BotPosition).where(
                BotPosition.status == "OPEN", BotPosition.deployment_id == deployment_id,
                BotPosition.dry_run.is_(False),
                BotPosition.symbol == sym, BotPosition.side == info["side"]))).scalars().first()
            if clash is not None:
                out["skipped_conflict"] += 1
                continue
            out["adopted"] += 1
            logger.warning("[keyed-import] ADOPTING untracked %s %s size=%.8g into deployment %s "
                           "(was unmanaged: no exit, no risk accounting)",
                           sym, info["side"], info["size"], deployment_id)
            if not dry_run:
                db.add(BotPosition(
                    symbol=sym, side=info["side"], size=info["size"],
                    entry_price=info["entry"] or 0.0, status="OPEN",
                    opened_at=info["opened_at"],
                    deployment_id=deployment_id,
                    unrealized_pnl=info["upnl"], pnl_source=None))

        # ---- 4. CORRECT sizes that disagree with the exchange ----
        # Nothing else refreshes size for a keyed account: sync_all_positions writes only
        # unrealized_pnl for a still-open row. A stale size feeds every later diff (a target book, a
        # close_pct reduce) a wrong "current", and a target diff never converges.
        for sym, db_size, exch_size in res.size_mismatch:
            row = by_symbol.get(sym)
            if row is None:
                continue
            out["size_fixed"] += 1
            logger.warning("[keyed-import] %s: size %.8g -> %.8g (exchange is authority)",
                           sym, db_size, exch_size)
            if not dry_run:
                row.size = exch_size
                if exch.get(sym, {}).get("entry"):
                    row.entry_price = exch[sym]["entry"]

        # ---- 5. REPAIR side flips ----
        # sync_all_positions REFUSES to close these (closing them generates orphans, fingerprint
        # pnl_source='venue_unmatched'); this is where the row is actually corrected.
        for sym in res.side_flip:
            row = by_symbol.get(sym)
            info = exch.get(sym)
            if row is None or info is None:
                continue
            clash = (await db.execute(select(BotPosition).where(
                BotPosition.status == "OPEN", BotPosition.deployment_id == row.deployment_id,
                BotPosition.dry_run.is_(False),
                BotPosition.symbol == sym, BotPosition.side == info["side"],
                BotPosition.id != row.id))).scalars().first()
            if clash is not None:
                out["skipped_conflict"] += 1
                logger.error("[keyed-import] %s: side flip %s->%s blocked by an existing OPEN row "
                             "(id=%s) — manual reconcile needed", sym, row.side, info["side"], clash.id)
                continue
            out["side_fixed"] += 1
            logger.warning("[keyed-import] %s: side %s -> %s, size -> %.8g (exchange is authority)",
                           sym, row.side, info["side"], info["size"])
            if not dry_run:
                row.side = info["side"]
                row.size = info["size"]
                if info["entry"]:
                    row.entry_price = info["entry"]

        if not dry_run:
            await db.commit()

    if out["adopted"] or out["size_fixed"] or out["side_fixed"] or out["ambiguous"]:
        logger.warning("[keyed-import] deployments %s: %s", sorted(deployment_ids), out)
    return out
