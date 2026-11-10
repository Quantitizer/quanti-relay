"""Take-profit ladder: several take-profit levels, each closing part of the position.

The user gives the levels: [{"price": 105, "close_pct": 50}, {"pct": 4, "close_pct": 50}] - each
either a price or a % from entry, and the % of the position (as it opened) it closes. The relay
never invents a level. On a live position each level is a reduce-only limit order resting on the
venue (it fills at its price or better and can never open or grow a position); the fixed stop,
trailing stop and break-even keep working beside it. Paper positions are filled by the relay when
the price reaches a level.

Pure functions only (no I/O): validation and the plan of prices and quantities.
"""
from __future__ import annotations

import math
from typing import Any

from relay.execution.rounding import round_price_to_tick, round_qty_to_step

_EPS = 1e-9


def ladder_problem(levels: Any) -> str | None:
    """Why these levels are unusable, or None. Checks what needs no price: each level has exactly
    one of price / pct (> 0) and a close_pct in (0, 100]; together they close at most 100%; % levels
    rise. Prices are checked against the entry by ladder_price_problem."""
    if not isinstance(levels, (list, tuple)) or not levels:
        return 'a take-profit ladder needs at least one level'
    total = 0.0
    last_pct = None
    for n, level in enumerate(levels, start=1):
        if not isinstance(level, dict):
            return f'take-profit level {n} must be an object'
        unknown = set(level) - {'price', 'pct', 'close_pct'}
        if unknown:
            return f'take-profit level {n}: unknown field(s) {sorted(unknown)}'
        price, pct, close = level.get('price'), level.get('pct'), level.get('close_pct')
        for name, v in (('price', price), ('pct', pct), ('close_pct', close)):
            if v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool)
                                      and math.isfinite(v)):
                return f'take-profit level {n}: {name} must be a number'
        if (price is None) == (pct is None):
            return f'take-profit level {n}: set exactly one of price, pct'
        if (price if price is not None else pct) <= 0:
            return f'take-profit level {n}: the price / pct must be more than 0'
        if close is None or not 0 < close <= 100:
            return f'take-profit level {n}: close_pct must be more than 0 and at most 100'
        if pct is not None:
            if last_pct is not None and pct <= last_pct:
                return f'take-profit level {n}: each % level must be further from entry than the one before'
            last_pct = pct
        total += close
    if total > 100 + _EPS:
        return f'the take-profit levels close {total:g}% of the position; at most 100%'
    return None


def level_price(side: str, entry: float, level: dict[str, Any], tick: float | None = None) -> float:
    """A level's price: its own, or entry +/- pct % (on the profit side), on the price tick."""
    if level.get('price') is not None:
        price = float(level['price'])
    else:
        sign = 1 if side.lower() == 'long' else -1
        price = entry * (1 + sign * float(level['pct']) / 100)
    return round_price_to_tick(price, tick)


def ladder_price_problem(side: str, entry: float, levels: list[dict[str, Any]]) -> str | None:
    """Every level on the profit side of `entry`, each further from it than the one before."""
    long_side = side.lower() == 'long'
    previous = entry
    for n, level in enumerate(levels, start=1):
        price = level_price(side, entry, level)
        if price <= 0:
            return f'take-profit level {n} would be at {price:g}: a price must stay above 0'
        if not (price > previous if long_side else price < previous):
            if previous == entry:
                return (f'take-profit level {n} ({price:g}) is not on the profit side of the entry '
                        f'{entry:g} for a {side.lower()}')
            return f'take-profit level {n} ({price:g}) must be further from entry than level {n - 1}'
        previous = price
    return None


def plan_ladder(side: str, entry: float, size: float, levels: list[dict[str, Any]], tick: float | None,
                qty_step: float | None, min_qty: float | None) -> list[dict[str, Any]]:
    """Price and quantity of each level for a position of `size`.

    Quantities follow the cumulative %: level n closes up to (sum of close_pct of levels 1..n) of
    the size, rounded DOWN to the venue's step, so the levels never add up to more than asked. A
    level whose share is below the venue minimum is 'skipped' and its share moves to the next
    level. Levels adding up to 100% close the whole (step-aligned) size.

    Returns [{'n', 'price', 'pct', 'close_pct', 'qty', 'status': 'planned' | 'skipped', 'note'}].
    """
    plan: list[dict[str, Any]] = []
    allocated = 0.0
    cumulative = 0.0
    for n, level in enumerate(levels, start=1):
        cumulative += float(level['close_pct'])
        qty = round(round_qty_to_step(size * cumulative / 100, qty_step) - allocated, 12)
        entry_out = {'n': n, 'price': level_price(side, entry, level, tick), 'pct': level.get('pct'),
                     'close_pct': float(level['close_pct']), 'qty': max(qty, 0.0), 'status': 'planned',
                     'note': None}
        if qty <= _EPS or (min_qty and qty < min_qty - _EPS):
            entry_out.update(qty=0.0, status='skipped',
                             note=f'below the venue minimum {min_qty:g}' if min_qty else 'nothing to close')
        else:
            allocated += qty
        plan.append(entry_out)
    return plan
