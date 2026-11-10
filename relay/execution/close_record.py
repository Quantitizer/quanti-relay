"""Recording a closed position, the same way on every close path.

The result recorded is, in order of preference:

1. venue_api   - the venue's own closed-PnL records for this position's hold window, complete
                 (their closed sizes add up to the position's full size, partial closes included;
                 see relay.execution.position_pnl_reconcile.reconcile_row).
2. local_calc  - a close the relay placed itself, priced from the real fills (the venue-confirmed
                 entry and exit prices); fees and funding are not in it yet. Only when the entry
                 price IS a venue fill: an entry recorded as expected_unconfirmed or mark_at_import
                 leaves the close pending.
3. pending     - nothing trustworthy yet (e.g. a close detected on the venue before its record is
                 published): exit price and PnL stay empty until the reconciler fills them in.

Paper positions are priced from their simulated fills ('dry_run') and never look at the venue.

Nothing is invented: no entry price reused as an exit price, no borrowed record, no guessed close
reason, no leverage written at close. PnL% is always the % of the position's value at entry.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from relay.execution.position_pnl_reconcile import reconcile_row, to_ms


def _local_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    return (exit_price - entry) * qty if str(side).upper() == 'LONG' else (entry - exit_price) * qty


async def partial_closes(conn, position_id: int) -> list[dict[str, Any]]:
    """The position's recorded partial closes: [{'qty', 'price', 'pnl'}] (price/pnl may be None)."""
    rows = await conn.fetch(
        "SELECT qty, price, metadata FROM trades WHERE position_id = $1 AND trade_type = 'partial_exit'",
        position_id,
    )
    out = []
    for r in rows:
        meta = r['metadata']
        if isinstance(meta, str):
            import json
            meta = json.loads(meta)
        out.append({'qty': float(r['qty']), 'price': r['price'], 'pnl': (meta or {}).get('pnl')})
    return out


async def entry_cost(conn, position_id: int) -> float | None:
    """What a position that was ADDED TO cost: the sum of qty x price of its entry trades. None
    when it has a single entry (entry price x total size is exact then) or a price is unknown.
    With adds after partial closes, entry price x total size mixes the average of different
    moments; this is the real cost base for PnL%."""
    rows = await conn.fetch(
        "SELECT qty, price FROM trades WHERE position_id = $1 AND trade_type = 'entry'", position_id)
    if len(rows) < 2 or any(r['price'] is None or r['qty'] is None for r in rows):
        return None
    return sum(float(r['qty']) * float(r['price']) for r in rows)


# Entry prices that are not a venue fill: no local PnL may be computed from them.
_UNCONFIRMED_ENTRY = ("expected_unconfirmed", "mark_at_import")


def _entry_confirmed(row: dict[str, Any]) -> bool:
    meta = row.get('entry_meta')
    if isinstance(meta, str):
        import json
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    return (meta or {}).get('entry_price_source') not in _UNCONFIRMED_ENTRY


async def price_close(db, client, row: dict[str, Any], closed_at: datetime, *,
                      exit_fill: float | None = None, paper: bool = False,
                      close_order_id: str | None = None) -> dict[str, Any]:
    """What to record for a position that is now closed. `row` needs id, symbol, side, entry_price,
    size (the size closed now), opened_at and (optional) entry_meta. `exit_fill` is the
    venue-confirmed fill of a close the relay placed (paper: the simulated fill). Database reads
    use short connections: no connection is held while the venue is queried. Returns
    {'exit_price', 'pnl', 'pnl_percent', 'pnl_source', 'close_order_id'}."""
    entry = float(row['entry_price'])
    size = float(row['size'])
    async with db.acquire() as conn:
        partials = await partial_closes(conn, row['id'])
        claimed_ids = await _claimed_order_ids(conn, row['symbol'], row['id'])
        cost = await entry_cost(conn, row['id'])
    original_qty = size + sum(p['qty'] for p in partials)
    notional = cost if cost is not None else entry * original_qty
    partial_pnls = [p['pnl'] for p in partials]
    known_partials = None if any(p is None for p in partial_pnls) else sum(partial_pnls)

    def _result(exit_price, pnl, source, order_id=None):
        pct = (pnl / notional * 100) if pnl is not None and notional > 0 else None
        return {'exit_price': exit_price, 'pnl': pnl, 'pnl_percent': pct, 'pnl_source': source,
                'close_order_id': order_id or close_order_id}

    if paper:
        if exit_fill is None or known_partials is None:
            return _result(exit_fill, None, 'pending')
        return _result(exit_fill, _local_pnl(row['side'], entry, exit_fill, size) + known_partials, 'dry_run')

    # 1. The venue's own records for the hold window, if complete.
    try:
        opened_at = row['opened_at']
        records = await client.get_closed_pnl(category='linear', symbol=row['symbol'], limit=100,
                                              start_time=to_ms(opened_at - timedelta(minutes=1)),
                                              max_pages=5) or []
        claimed = set(claimed_ids)
        out = reconcile_row(records, None, row['symbol'], opened_at, closed_at, claimed,
                            position_side=row['side'], expected_qty=original_qty)
        if out['source'] == 'venue_api':
            return _result(out['exit_price'], out['pnl'], 'venue_api',
                           close_order_id or (out['order_ids'][-1] if out['order_ids'] else None))
        if out['source'] in ('incomplete', 'ambiguous'):
            logger.info(f"[Close] {row['symbol']} #{row['id']}: venue records {out['source']} - "
                        f"left for the reconciler")
    except Exception as e:
        logger.warning(f"[Close] {row['symbol']} #{row['id']}: venue closed-PnL lookup failed: {e}")

    # 2. A close the relay placed: priced from the real fills (the entry must be a fill too).
    if exit_fill is not None and known_partials is not None and _entry_confirmed(row):
        return _result(exit_fill, _local_pnl(row['side'], entry, exit_fill, size) + known_partials, 'local_calc')

    # 3. Nothing trustworthy yet.
    return _result(exit_fill, None, 'pending')


async def _claimed_order_ids(conn, symbol: str, position_id: int) -> list[str]:
    rows = await conn.fetch(
        "SELECT close_order_id FROM positions WHERE symbol = $1 AND id <> $2 AND close_order_id IS NOT NULL",
        symbol, position_id,
    )
    return [r['close_order_id'] for r in rows]


async def write_close(conn, row: dict[str, Any], priced: dict[str, Any], closed_at: datetime,
                      reason: str, *, only_if_open: bool = True) -> bool:
    """Mark the row CLOSED with `priced` and add its exit trade (with the exit reason). With
    `only_if_open`, a row another writer already closed is left alone. Returns True if written."""
    guard = " AND status IN ('OPEN', 'CLOSING')" if only_if_open else ""
    status = await conn.execute(
        "UPDATE positions SET status = 'CLOSED', exit_price = $1, closed_at = $2, pnl = $3, "
        "pnl_percent = $4, close_order_id = COALESCE($5, close_order_id), pnl_source = $6, "
        "updated_at = $2 WHERE id = $7" + guard,
        priced['exit_price'], closed_at, priced['pnl'], priced['pnl_percent'],
        priced['close_order_id'], priced['pnl_source'], row['id'],
    )
    if status.endswith(" 0"):
        return False
    await conn.execute(
        "INSERT INTO trades (position_id, symbol, trade_type, side, qty, price, time, order_id, "
        "exit_reason, created_at, updated_at) VALUES ($1, $2, 'exit', $3, $4, $5, $6, $7, $8, $6, $6)",
        row['id'], row['symbol'], 'SELL' if str(row['side']).upper() == 'LONG' else 'BUY',
        float(row['size']), priced['exit_price'], closed_at, priced['close_order_id'], reason,
    )
    return True
