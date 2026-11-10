"""
Exchange rounding helpers — Bybit rejects orders whose qty is not a multiple of qtyStep
or whose (stop) price is not a multiple of tickSize. Pure math, unit-tested.

Rounding directions are chosen for SAFETY:
- qty rounds DOWN to qtyStep (never exceed the intended notional / gross budget); below
  min_qty -> 0 (skip as dust rather than send an invalid order).
- a stop-loss rounds TOWARD entry (long stop up, short stop down) so rounding can only
  TIGHTEN the stop (reduce risk), never loosen it past the requested distance.
"""
from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

_EPS = 1e-9


def _decimals_for_step(step: float | None) -> int:
    """Number of fractional digits implied by a qtyStep/tickSize (0.001 -> 3, 1 -> 0, 10 -> 0)."""
    if not step or step <= 0:
        return 8                                  # unknown step -> generous precision, still finite
    d = Decimal(str(step)).normalize()
    exp = d.as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def format_qty(qty: float, step: float | None) -> str:
    """Bybit-safe qty STRING: rounded DOWN to `step` and rendered at the step's exact decimal
    precision, so no float artifact ('5000.0000001') or scientific notation ('1e-05') reaches the
    API (both trigger 'Qty invalid'). Pairs with round_qty_to_step (same down-rounding)."""
    decimals = _decimals_for_step(step)
    q = round_qty_to_step(qty, step)
    quant = Decimal(1).scaleb(-decimals)          # e.g. decimals=3 -> Decimal('0.001')
    return format(Decimal(str(q)).quantize(quant, rounding=ROUND_DOWN), "f")   # 'f' -> never sci-notation


def format_price(price: float, tick: float | None) -> str:
    """Bybit-safe price STRING at the tickSize's exact decimal precision — for a stop already
    tick-aligned by round_stop_to_tick, strips any float artifact / sci-notation before it hits the
    API (which also rejects those). Not for re-rounding; the caller aligns to tick first."""
    if not price or price <= 0:
        return str(price)
    decimals = _decimals_for_step(tick)
    return format(Decimal(str(price)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP), "f")


def round_qty_to_step(qty: float, step: float | None, min_qty: float | None = None) -> float:
    """Round qty DOWN to a multiple of `step`. Returns 0.0 if below `min_qty` (dust)."""
    if qty <= 0:
        return 0.0
    q = qty if not step or step <= 0 else math.floor(qty / step + _EPS) * step
    q = round(q, 12)
    if min_qty and q < min_qty - _EPS:
        return 0.0
    return q


def round_stop_to_tick(stop_price: float, tick: float | None, side: str) -> float:
    """Round a stop price to a valid tick, TOWARD entry (long->up, short->down) so risk
    can only shrink. `side` is the POSITION side ('long'/'buy' or 'short'/'sell')."""
    if not tick or tick <= 0 or stop_price <= 0:
        return stop_price
    is_long = str(side).strip().lower() in ("long", "buy")
    n = stop_price / tick
    p = (math.ceil(n - _EPS) if is_long else math.floor(n + _EPS)) * tick
    return round(p, 12)


def round_price_to_tick(price: float, tick: float | None) -> float:
    """Round any price to the nearest tick (for limit/reference prices)."""
    if not tick or tick <= 0 or price <= 0:
        return price
    return round(round(price / tick) * tick, 12)
