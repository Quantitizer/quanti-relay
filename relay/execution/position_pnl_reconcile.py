"""Authoritative per-position P&L + funding reconciliation — pure core.

A close placed by the relay records no realized P&L on the row; matching it to a Bybit
`closed-pnl` record by ENTRY-PRICE PROXIMITY (no stable key) misses funding and drops partial-trim
P&L. This module replaces that guesswork with a WINDOW-SUM keyed on the position's own hold interval:

  * realized trade P&L  = Σ closedPnl of every closed-pnl record whose fill time ∈ [opened, closed].
    A reduce-only TRIM emits its OWN closed-pnl record, so the window-sum captures trims for free —
    a single best match misses them.
  * funding             = Σ `change` of every SETTLEMENT transaction-log row for the symbol ∈ window.
  * double-counting     = a shared `claimed` set of orderIds ensures each closed-pnl record is
    attributed to at most ONE position row across overlapping open→close cycles.

Pure + fully unit-testable (no I/O). The resolver feeds it the fetched Bybit records; it never places
or cancels an order — it only reads exchange truth and computes what to record.
"""
from __future__ import annotations

from datetime import UTC, datetime


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _rec_ms(rec: dict) -> int | None:
    """Epoch-ms of a Bybit record: closed-pnl `updatedTime`/`createdTime` or txlog `transactionTime`."""
    for k in ("updatedTime", "transactionTime", "createdTime"):
        v = rec.get(k)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def to_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def closing_side(position_side: str) -> str:
    """The venue order side that closes a position: a LONG is closed by a Sell, a SHORT by a Buy."""
    return "Sell" if str(position_side).upper() in ("LONG", "BUY") else "Buy"


def sum_closed_pnl_in_window(records: list[dict], start_ms: int, end_ms: int,
                             claimed: set | None = None, close_side: str | None = None) -> dict:
    """Σ closedPnl over the hold window, claiming each orderId once. Captures trims + final close.
    `close_side` ('Sell' for a long) keeps only closes of THIS position's direction.
    Returns {pnl, exit_price, order_ids, n}. `exit_price` is the LAST (latest-time) record's exit."""
    claimed = claimed if claimed is not None else set()
    picks = []
    for r in records:
        t = _rec_ms(r)
        if t is None or t < start_ms or t > end_ms:
            continue
        if close_side and r.get("side") and r.get("side") != close_side:
            continue
        oid = r.get("orderId")
        if oid and oid in claimed:
            continue
        picks.append((t, r, oid))
    picks.sort(key=lambda x: x[0])
    pnl, exit_price, order_ids, closed_qty = 0.0, None, [], 0.0
    for _, r, oid in picks:
        pnl += _f(r.get("closedPnl"))
        closed_qty += _f(r.get("closedSize"))
        ep = _f(r.get("avgExitPrice"))
        if ep > 0:
            exit_price = ep
        if oid:
            claimed.add(oid)
            order_ids.append(oid)
    return {"pnl": round(pnl, 8), "exit_price": exit_price, "order_ids": order_ids, "n": len(picks),
            "closed_qty": round(closed_qty, 12)}


def sum_funding_in_window(txlog: list[dict], symbol: str, start_ms: int, end_ms: int) -> dict:
    """Σ `change` of SETTLEMENT (funding) txlog rows for `symbol` within the hold window. `change` is
    the signed wallet delta (received +, paid −) = ground-truth funding cash flow. Returns {funding, n}."""
    funding, n = 0.0, 0
    for r in txlog:
        if str(r.get("type") or "").upper() != "SETTLEMENT":
            continue
        if symbol and str(r.get("symbol") or "") != symbol:
            continue
        t = _rec_ms(r)
        if t is None or t < start_ms or t > end_ms:
            continue
        funding += _f(r.get("change"))
        n += 1
    return {"funding": round(funding, 8), "n": n}


def reconcile_row(closed_pnl_records: list[dict], txlog: list[dict] | None, symbol: str,
                  opened_at: datetime, closed_at: datetime, claimed: set | None = None,
                  end_buffer_ms: int = 120_000, position_side: str | None = None,
                  expected_qty: float | None = None) -> dict:
    """Authoritative reconciliation for ONE closed position. Windows on [opened_at, closed_at+buffer]
    (buffer covers a settlement landing on the close boundary). `position_side` restricts the
    closes to this position's direction. `expected_qty` (the position's full size, partial closes
    included) is a completeness check: the window's closed sizes must add up to it, else the venue
    has not published every close yet ('incomplete') or the window holds another position's closes
    too ('ambiguous') - neither is priced. `txlog=None` means the funding log could not be read:
    funding is then None (unknown), never 0. Returns what to record:
    {pnl, funding, total, exit_price, source, order_ids, n_closes, n_funding}."""
    start_ms = to_ms(opened_at)
    end_ms = to_ms(closed_at)
    if start_ms is None or end_ms is None:
        return {"pnl": None, "funding": None, "total": None, "exit_price": None,
                "source": "unresolvable", "order_ids": [], "n_closes": 0, "n_funding": 0}
    end_ms += end_buffer_ms
    cp = sum_closed_pnl_in_window(closed_pnl_records, start_ms, end_ms, claimed,
                                  close_side=closing_side(position_side) if position_side else None)
    fn = sum_funding_in_window(txlog, symbol, start_ms, end_ms) if txlog is not None else None
    funding = fn["funding"] if fn is not None else None
    if cp["n"] == 0:
        # No closed-pnl in window — can't authoritatively price the trade leg; leave for a later pass.
        return {"pnl": None, "funding": (funding if fn and fn["n"] else None), "total": None,
                "exit_price": None, "source": "no_match", "order_ids": [],
                "n_closes": 0, "n_funding": fn["n"] if fn else 0}
    if expected_qty is not None:
        tol = max(1e-9, expected_qty * 1e-6)
        if abs(cp["closed_qty"] - expected_qty) > tol:
            # The order ids stay unclaimed, so a later pass (or another row) can still use them.
            for oid in cp["order_ids"]:
                if claimed is not None:
                    claimed.discard(oid)
            return {"pnl": None, "funding": None, "total": None, "exit_price": None,
                    "source": "incomplete" if cp["closed_qty"] < expected_qty else "ambiguous",
                    "order_ids": [], "n_closes": cp["n"], "n_funding": 0}
    total = round(cp["pnl"] + funding, 8) if funding is not None else None
    return {"pnl": cp["pnl"], "funding": funding, "total": total,
            "exit_price": cp["exit_price"], "source": "venue_api", "order_ids": cp["order_ids"],
            "n_closes": cp["n"], "n_funding": fn["n"] if fn else 0}
