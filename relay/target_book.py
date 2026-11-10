"""Target book planning: from a target signal's weights to the trades that reach them (pure, no I/O).

    weights  (signed fractions of equity, from the signal; symbols left out go to zero)
      -> target notionals      weight x equity x gross_leverage (the signal's optional multiplier)
      -> the rails             symbols off the lists dropped; each leg clipped to max_notional; the
                               whole book scaled down to max_gross_leverage (its shape kept)
      -> venue minimums        legs too small for the venue dropped; with the neutralise option the
                               book is neutralised and re-checked until both hold (trimming the
                               larger side can push a leg below the minimum)
      -> actions vs the book   enter / exit / resize (up or down) / flip / hold

A same-side change smaller than the deployment's min_change_pct (of equity), or too small for the
venue to execute, is held, not traded. Nothing here is preset: every limit is the
user's. Carried over from the predecessor's rebalance functions, without its strategy and its
venue-specific compliance rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from relay.execution.rounding import round_qty_to_step
from relay.rails import symbol_refusal


@dataclass
class Leg:
    """A position in the book: its side and current value (size x current price)."""
    side: str            # 'long' | 'short'
    size: float
    notional: float


@dataclass
class Action:
    action: str          # 'enter' | 'exit' | 'resize' | 'flip' | 'hold'
    symbol: str          # unified
    side: str | None     # the target side (exit: the side being closed)
    target_notional: float
    current_notional: float
    note: str | None = None


def target_notionals(weights: dict[str, float], equity: float, gross_leverage: float | None = None) -> dict[str, float]:
    """Signed target value per symbol (+ long, - short); zero weights are left out."""
    scale = equity * (gross_leverage if gross_leverage is not None else 1.0)
    return {s: w * scale for s, w in weights.items() if w * scale != 0}


def apply_rails(target: dict[str, float], rails: dict[str, Any] | None, equity: float) -> tuple[dict[str, float], list[str]]:
    """The deployment's rails on the whole book: legs on no list dropped, each leg clipped to
    max_notional_usd / max_notional_pct, then the book scaled down (all legs alike, so its shape is
    kept) to max_gross_leverage. Returns (target, notes)."""
    r = rails or {}
    notes: list[str] = []
    out: dict[str, float] = {}
    for s, v in target.items():
        why = symbol_refusal(r, s)
        if why:
            notes.append(f"{s} dropped: {why}")
            continue
        caps = [c for c in (r.get("max_notional_usd"),
                            equity * r["max_notional_pct"] / 100 if r.get("max_notional_pct") is not None else None)
                if c is not None]
        if caps and abs(v) > min(caps):
            notes.append(f"{s} clipped from {abs(v):.2f} to {min(caps):.2f} (max_notional)")
            v = min(caps) if v > 0 else -min(caps)
        out[s] = v
    if r.get("max_gross_leverage") is not None:
        cap = r["max_gross_leverage"] * equity
        gross = sum(abs(v) for v in out.values())
        if gross > cap > 0:
            k = cap / gross
            out = {s: v * k for s, v in out.items()}
            notes.append(f"book scaled by {k:.4f} to max_gross_leverage {r['max_gross_leverage']:g}")
    return out, notes


def neutralize_net(target: dict[str, float]) -> dict[str, float]:
    """Trim the larger side so long value == short value. Only ever shrinks, so no limit above is
    broken again. A one-sided book is left as it is."""
    long_gross = sum(v for v in target.values() if v > 0)
    short_gross = -sum(v for v in target.values() if v < 0)
    if long_gross <= 0 or short_gross <= 0 or long_gross == short_gross:
        return dict(target)
    if long_gross > short_gross:
        k = short_gross / long_gross
        return {s: (v * k if v > 0 else v) for s, v in target.items()}
    k = long_gross / short_gross
    return {s: (v * k if v < 0 else v) for s, v in target.items()}


def drop_below_minimum(target: dict[str, float], prices: dict[str, float], specs: dict[str, dict],
                       neutralize: bool) -> tuple[dict[str, float], list[str]]:
    """Legs the venue could not execute (below its minimum quantity or order value at the current
    price, or with no price or lot rules known) are dropped. With `neutralize`, the book is
    neutralised and checked again until no leg drops (each round only shrinks or drops legs, so it
    ends). Returns (target, notes)."""
    notes: list[str] = []
    book = neutralize_net(target) if neutralize else dict(target)
    while True:
        kept: dict[str, float] = {}
        for s, v in book.items():
            px, spec = prices.get(s), specs.get(s)
            if not px or px <= 0 or not spec:
                notes.append(f"{s} dropped: no price or venue lot rules")
            elif not executable(abs(v), px, spec):
                notes.append(f"{s} dropped: {abs(v):.2f} is below the venue minimum")
            else:
                kept[s] = v
        if not neutralize or len(kept) == len(book):
            return kept, notes
        book = neutralize_net(kept)


def executable(value: float, price: float, spec: dict) -> bool:
    """Whether an order of `value` at `price` passes the venue's lot rules (step, minimum quantity,
    minimum order value)."""
    qty = round_qty_to_step(value / price, spec.get("qty_step"))
    return qty > 0 and qty >= (spec.get("min_qty") or 0) and value >= (spec.get("min_notional") or 0)


def plan_actions(target: dict[str, float], current: dict[str, Leg], equity: float,
                 min_change_pct: float | None, prices: dict[str, float] | None = None,
                 specs: dict[str, dict] | None = None) -> list[Action]:
    """Target values vs the current book: enter (new), exit (left out), flip (other side), resize
    (same side, a change of at least min_change_pct of equity - empty = any change - that the
    venue can execute at `prices` with `specs`) or hold."""
    min_change = (min_change_pct or 0) / 100 * equity
    actions: list[Action] = []
    for s, v in target.items():
        side = "long" if v > 0 else "short"
        leg = current.get(s)
        if leg is None:
            actions.append(Action("enter", s, side, abs(v), 0.0))
        elif leg.side != side:
            actions.append(Action("flip", s, side, abs(v), leg.notional))
        elif (abs(abs(v) - leg.notional) > min_change and abs(v) != leg.notional
              and (prices is None or specs is None or s not in specs
                   or executable(abs(abs(v) - leg.notional), prices[s], specs[s]))):
            actions.append(Action("resize", s, side, abs(v), leg.notional))
        else:
            actions.append(Action("hold", s, side, abs(v), leg.notional))
    for s, leg in current.items():
        if s not in target:
            actions.append(Action("exit", s, leg.side, 0.0, leg.notional))
    return actions


def execution_order(actions: list[Action]) -> list[Action]:
    """What frees margin first: exits, then reductions, then flips (each closes before it opens),
    then increases and new entries. Holds are left out."""
    def rank(a: Action) -> int:
        if a.action == "exit":
            return 0
        if a.action == "resize" and a.target_notional < a.current_notional:
            return 1
        if a.action == "flip":
            return 2
        if a.action == "resize":
            return 3
        return 4                                          # enter
    return sorted((a for a in actions if a.action != "hold"), key=lambda a: (rank(a), a.symbol))
