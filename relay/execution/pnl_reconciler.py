"""Authoritative realized-PnL reconciliation for closed live positions.

A position closed by the relay or on the venue (stop hit, liquidation, manual close) is recorded
before the venue has published its realized PnL. This reconciler prices every closed row that is not
yet venue-verified by WINDOW-SUMMING the venue's closed-PnL records of the symbol over the
position's own hold interval [opened_at, closed_at] (see relay.execution.position_pnl_reconcile):
partial closes are captured, funding is added from the transaction log, and each venue order is
attributed to at most one row. Only closes in the position's direction count (a LONG is closed by a
Sell). The relay holds at most one live position per symbol per account, so the window is that
position's alone.

It never guesses: a row with no venue record in its window stays as it is for a later pass, and a
funding log that could not be read leaves funding unknown (NULL), never 0.

`dry_run=True` (the default) reports what it WOULD write and writes nothing. Read-only on the
exchange: it places or cancels nothing.
"""
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from relay.db.models import Account, Deployment, Position, Trade
from relay.execution.position_pnl_reconcile import reconcile_row, to_ms
from relay.venue import venue_of

logger = logging.getLogger(__name__)

# Closed rows that are NOT yet venue-verified (or still missing funding) — the reconcile targets.
_UNPRICED_SOURCES = ("pending", "default_zero", "ticker_fallback", "venue_unmatched", "local_calc")


def _default_account_clause(venue: str = "bybit"):
    """Keep ONLY default-account rows (no deployment OR a deployment on the process-wide account of
    this venue: no credentials_ref), so a default-client reconciliation can never price another
    account's rows with the wrong account's records."""
    default_deployments = (select(Deployment.id)
                           .join(Account, Account.id == Deployment.account_id)
                           .where(Account.venue == venue, Account.credentials_ref.is_(None)))
    return or_(Position.deployment_id.is_(None), Position.deployment_id.in_(default_deployments))


async def reconcile_closed_pnl(db_handle, bybit_client, *, dry_run: bool = True, lookback_days: int = 7,
                              limit: int = 1000, deployment_id: int | None = None,
                              max_pages: int = 20) -> dict:
    """Price not-yet-verified closed rows from the venue (see the module doc). Rows are processed
    oldest-first so the shared `claimed` set fills chronologically; orders already attributed to a
    verified row are claimed up front. A verified row that only lacks funding gets funding, never a
    new PnL. MULTI-ACCOUNT: pass `deployment_id` AND that deployment's own client; the default
    (None) is fenced to the default account."""
    since = datetime.now(UTC) - timedelta(days=lookback_days)
    since_ms = to_ms(since)
    report = {"dry_run": dry_run, "lookback_days": lookback_days, "scanned": 0,
              "reconciled": [], "no_match": [], "written": 0,
              "funding_total": 0.0, "pnl_total": 0.0, "funding_unavailable": False}

    # The transaction log is account-wide: fetched once for the whole window.
    try:
        txlog = await bybit_client.get_transaction_log(category="linear", start_time=since_ms) or []
    except Exception as e:
        logger.warning("[pnl-reconcile] transaction-log fetch failed - funding left unknown: %s", e)
        txlog = None
        report["funding_unavailable"] = True

    cp_cache: dict[str, list] = {}
    async with db_handle.session() as db:
        scope = (Position.deployment_id == deployment_id if deployment_id is not None
                 else _default_account_clause(venue_of(bybit_client)))
        claimed = set((await db.execute(select(Position.close_order_id).where(
            Position.pnl_source == "venue_api", Position.close_order_id.isnot(None), scope,
        ))).scalars().all())
        rows = (await db.execute(
            select(Position).where(
                Position.status == "CLOSED",
                Position.dry_run.is_(False),   # paper rows would match real closes
                Position.closed_at.isnot(None),
                Position.closed_at >= since,
                Position.opened_at.isnot(None),
                or_(Position.pnl_source.in_(_UNPRICED_SOURCES), Position.funding.is_(None)),
                scope,
            ).order_by(Position.opened_at.asc()).limit(limit)
        )).scalars().all()
        report["scanned"] = len(rows)

        for row in rows:
            sym = row.symbol
            if sym not in cp_cache:
                try:
                    cp_cache[sym] = await bybit_client.get_closed_pnl(
                        category="linear", symbol=sym, limit=100, start_time=since_ms,
                        max_pages=max_pages) or []
                except Exception as e:
                    logger.warning("[pnl-reconcile] closed-pnl fetch failed for %s: %s", sym, e)
                    cp_cache[sym] = None
            if cp_cache[sym] is None:
                report["no_match"].append({"id": row.id, "symbol": sym, "source": "fetch_failed"})
                continue
            verified = row.pnl_source == "venue_api"
            partial_qty = (await db.execute(select(func.coalesce(func.sum(Trade.qty), 0.0)).where(
                Trade.position_id == row.id, Trade.trade_type == "partial_exit"))).scalar_one()
            out = reconcile_row(cp_cache[sym], txlog, sym, row.opened_at, row.closed_at,
                                claimed if not verified else set(), position_side=row.side,
                                expected_qty=None if verified else float(row.size) + float(partial_qty))
            base = {"id": row.id, "symbol": sym, "side": row.side,
                    "opened_at": row.opened_at.isoformat(), "closed_at": row.closed_at.isoformat(),
                    **{k: out[k] for k in ("pnl", "funding", "total", "n_closes", "n_funding", "source")}}
            if verified:
                # Already venue-verified: only funding may be added, the PnL stays.
                if out["funding"] is not None:
                    report["reconciled"].append({**base, "funding_only": True})
                    report["funding_total"] += out["funding"]
                    if not dry_run:
                        row.funding = out["funding"]
                        report["written"] += 1
                continue
            if out["source"] == "venue_api":
                report["reconciled"].append(base)
                report["pnl_total"] += out["pnl"]
                report["funding_total"] += out["funding"] or 0.0
                if not dry_run:
                    row.pnl = out["pnl"]
                    entries = (await db.execute(select(Trade.qty, Trade.price).where(
                        Trade.position_id == row.id, Trade.trade_type == "entry"))).all()
                    if len(entries) > 1 and all(q is not None and p is not None for q, p in entries):
                        notional = sum(float(q) * float(p) for q, p in entries)     # added to: the real cost
                    else:
                        notional = float(row.entry_price) * (float(row.size) + float(partial_qty))
                    row.pnl_percent = out["pnl"] / notional * 100 if notional > 0 else None
                    if out["funding"] is not None:
                        row.funding = out["funding"]
                    if out["exit_price"]:
                        row.exit_price = out["exit_price"]
                    row.pnl_source = "venue_api"
                    report["written"] += 1
            else:
                report["no_match"].append(base)

        if not dry_run and report["written"]:
            await db.commit()

    report["pnl_total"] = round(report["pnl_total"], 4)
    report["funding_total"] = round(report["funding_total"], 4)
    logger.warning(
        "[pnl-reconcile] dry_run=%s scanned=%d reconciled=%d no_match=%d written=%d pnl=%.2f funding=%.2f",
        dry_run, report["scanned"], len(report["reconciled"]), len(report["no_match"]),
        report["written"], report["pnl_total"], report["funding_total"])
    return report


async def reconcile_partial_pnl(db_handle, bybit_client, *, dry_run: bool = True, lookback_days: int = 7,
                                deployment_id: int | None = None) -> dict:
    """Price partial closes recorded without a PnL (their fill was not confirmed, or the entry
    price was not a venue fill) from the venue's closed-PnL record of THAT order (matched by order
    id, so never a guess). Live, relay-placed partials only; writes when not dry_run."""
    since = datetime.now(UTC) - timedelta(days=lookback_days)
    report = {"dry_run": dry_run, "scanned": 0, "priced": [], "written": 0}
    async with db_handle.session() as db:
        scope = (Position.deployment_id == deployment_id if deployment_id is not None
                 else _default_account_clause(venue_of(bybit_client)))
        rows = (await db.execute(
            select(Trade, Position.symbol).join(Position, Position.id == Trade.position_id).where(
                Trade.trade_type == "partial_exit", Trade.order_id.isnot(None), Trade.time >= since,
                Position.dry_run.is_(False), scope,
            )
        )).all()
        todo = [(t, sym) for t, sym in rows if (t.metadata_ or {}).get("pnl") is None]
        report["scanned"] = len(todo)
        for trade, symbol in todo:
            try:
                records = await bybit_client.get_closed_pnl(
                    category="linear", symbol=symbol, limit=100,
                    start_time=to_ms(trade.time - timedelta(minutes=1)), max_pages=2) or []
            except Exception as e:
                logger.warning("[pnl-reconcile] closed-pnl fetch failed for %s: %s", symbol, e)
                continue
            # Every record of THAT order (a venue may split one order's close); all must carry a PnL,
            # and together they must account for exactly the partial's quantity.
            match = [r for r in records if r.get("orderId") == trade.order_id]
            if not match or any(r.get("closedPnl") in (None, "") for r in match):
                continue
            closed = sum(float(r.get("closedSize") or 0) for r in match)
            if abs(closed - float(trade.qty)) > max(1e-9, float(trade.qty) * 1e-6):
                continue
            pnl = sum(float(r["closedPnl"]) for r in match)
            exit_price = float(match[-1].get("avgExitPrice") or 0) or None
            report["priced"].append({"trade_id": trade.id, "symbol": symbol, "pnl": pnl})
            if not dry_run:
                trade.metadata_ = {**(trade.metadata_ or {}), "pnl": pnl, "pnl_source": "venue_api"}
                if exit_price and trade.price is None:
                    trade.price = exit_price
                report["written"] += 1
        await _price_venue_reduces(db, bybit_client, scope, since, dry_run, report)
        if not dry_run and report["written"]:
            await db.commit()
    return report


async def _price_venue_reduces(db, bybit_client, scope, since, dry_run: bool, report: dict) -> None:
    """Partials the drift repair recorded for a reduce made on the venue ('reduced_on_venue': no
    order of the relay's own). Priced from the venue's closed-PnL records of the symbol, in the
    position's direction, between the position's previous trade and the repair, of orders no
    record accounts for (not a trade's order, not a ladder order, not claimed by another such
    partial); together they must account for exactly the partial's quantity, else it waits."""
    rows = (await db.execute(
        select(Trade, Position).join(Position, Position.id == Trade.position_id).where(
            Trade.trade_type == "partial_exit", Trade.order_id.is_(None), Trade.exit_reason == "reduced_on_venue",
            Trade.time >= since, Position.dry_run.is_(False), scope,
        ).order_by(Trade.time)
    )).all()
    todo = [(t, p) for t, p in rows if (t.metadata_ or {}).get("pnl") is None]
    report["scanned"] += len(todo)
    for trade, pos in todo:
        known = set((await db.execute(
            select(Trade.order_id).where(Trade.symbol == trade.symbol, Trade.order_id.isnot(None),
                                         Trade.time >= since - timedelta(days=1))
        )).scalars().all())
        for other in (await db.execute(
                select(Trade.metadata_).where(Trade.symbol == trade.symbol, Trade.exit_reason == "reduced_on_venue",
                                              Trade.id != trade.id))).scalars().all():
            known.update((other or {}).get("venue_orders") or [])
        for lv in ((pos.management or {}).get("take_profits") or {}).get("levels") or []:
            if lv.get("order_id"):
                known.add(lv["order_id"])
        previous = (await db.execute(
            select(func.max(Trade.time)).where(Trade.position_id == pos.id, Trade.time < trade.time)
        )).scalar() or pos.opened_at or trade.time - timedelta(days=1)
        start, end = to_ms(previous - timedelta(minutes=1)), to_ms(trade.time)
        try:
            records = await bybit_client.get_closed_pnl(category="linear", symbol=trade.symbol, limit=100,
                                                         start_time=start, max_pages=2) or []
        except Exception as e:
            logger.warning("[pnl-reconcile] closed-pnl fetch failed for %s: %s", trade.symbol, e)
            continue
        closing = "Sell" if str(pos.side).upper() == "LONG" else "Buy"
        match = [r for r in records if r.get("side") == closing and r.get("orderId") not in known
                 and start <= int(r.get("updatedTime") or 0) <= end]
        if not match or any(r.get("closedPnl") in (None, "") for r in match):
            continue
        closed = sum(float(r.get("closedSize") or 0) for r in match)
        if abs(closed - float(trade.qty)) > max(1e-9, float(trade.qty) * 1e-6):
            continue
        pnl = sum(float(r["closedPnl"]) for r in match)
        exit_value = sum(float(r.get("avgExitPrice") or 0) * float(r.get("closedSize") or 0) for r in match)
        report["priced"].append({"trade_id": trade.id, "symbol": trade.symbol, "pnl": pnl})
        if not dry_run:
            trade.metadata_ = {**(trade.metadata_ or {}), "pnl": pnl, "pnl_source": "venue_api",
                               "venue_orders": [r.get("orderId") for r in match]}
            if exit_value > 0 and trade.price is None:
                trade.price = exit_value / closed
            report["written"] += 1
