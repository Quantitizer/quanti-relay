"""
Position sizer — turns a signal's `size` object into an order quantity (signal contract v1, §3).

Size modes (exactly one per signal):
  - {"notional_usd": 500}  position notional in USD
  - {"equity_pct": 2}      position notional as % of deployment equity
  - {"risk_pct": 0.5}      % of equity LOST if the stop is hit; needs a stop price
  - {"qty": 0.01}          base-asset quantity, used as-is

Pure function, no I/O: the caller supplies price, equity and stop. The result is the RAW quantity;
the caller rounds it to the venue's qtyStep (`rounding.round_qty_to_step`) right before placing.
Linear (USD-margined) contracts only.
"""
from __future__ import annotations

from dataclasses import dataclass

SIZE_MODES = ("notional_usd", "equity_pct", "risk_pct", "qty")


@dataclass(frozen=True)
class SizeResult:
    quantity: float          # base-asset units, unrounded; 0.0 when the order must not be sent
    notional: float          # quantity × entry_price, USD
    equity_pct: float        # notional as % of equity (0.0 if equity is unknown)
    clipped: bool            # True if max_notional shrank the order
    reason: str


def _skip(reason: str) -> SizeResult:
    return SizeResult(0.0, 0.0, 0.0, False, reason)


def compute_size(
    size: dict,
    entry_price: float,
    equity: float,
    stop_price: float | None = None,
    max_notional: float | None = None,
    min_notional: float | None = None,
) -> SizeResult:
    """Quantity for one order. Never raises on bad input — returns quantity 0 with a reason."""
    if entry_price is None or entry_price <= 0:
        return _skip(f"invalid entry_price: {entry_price}")

    modes = [m for m in SIZE_MODES if m in size]
    if len(modes) != 1:
        return _skip(f"size must contain exactly one of {SIZE_MODES}, got {sorted(size)}")
    mode = modes[0]
    try:
        value = float(size[mode])
    except (TypeError, ValueError):
        return _skip(f"size.{mode} is not a number: {size[mode]!r}")
    if value <= 0:
        return _skip(f"size.{mode} must be > 0, got {value}")

    if mode in ("equity_pct", "risk_pct") and (equity is None or equity <= 0):
        return _skip(f"size.{mode} needs a positive equity, got {equity}")

    if mode == "notional_usd":
        notional = value
    elif mode == "equity_pct":
        notional = equity * value / 100.0
    elif mode == "risk_pct":
        if not stop_price or stop_price <= 0:
            return _skip("size.risk_pct requires a stop price")
        stop_dist = abs(entry_price - stop_price) / entry_price
        if stop_dist <= 0:
            return _skip("size.risk_pct: stop equals entry price")
        notional = equity * value / 100.0 / stop_dist
    else:  # qty
        notional = value * entry_price

    clipped = False
    if max_notional is not None and notional > max_notional:
        notional = max_notional
        clipped = True

    # No assumed minimum: the engine checks the venue's own published minimum order value.
    if min_notional is not None and notional < min_notional:
        return _skip(f"notional {notional:.2f} below minimum {min_notional:.2f}")

    quantity = round(notional / entry_price, 12)
    pct = round(notional / equity * 100.0, 4) if equity and equity > 0 else 0.0
    return SizeResult(quantity, notional, pct, clipped,
                      f"{mode}={value:g}" + (f", clipped to {max_notional:g}" if clipped else ""))
