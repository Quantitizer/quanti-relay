"""NOT WIRED YET: the running relay does not use this module. It is kept for phase 3 (optional maker execution; its tuning numbers become user settings), and reviewed and fixed when it is connected (tests/test_unwired_modules.py keeps the running relay from importing it before then).


SmartExecutor — lean maker-chase (patient / maker execution).

WHY: for orders that are small relative to a market's traded volume, market impact is negligible
and most of the execution cost is the taker fee. So this does NOT slice for impact — it works a
single post-only limit per leg at the passive touch, CHASES it toward the touch to fill as maker,
and MARKET-SWEEPS any residual at the deadline so the book always reaches its exact target
(reaching the target matters more than perfect fees).

Design: pure orchestration with every exchange/DB collaborator INJECTED (SmartExecDeps) + an
injectable clock/sleep, so it is fully unit-tested with fakes and never touches a live exchange in
tests. Sizing and target selection happen upstream — this only changes HOW fills arrive.

Stops: the post-only limit carries the native SL atomically (activates on fill -> never naked);
the SL is re-anchored to the actual avg fill at the end; a guardian halt ABORTS immediately (its
market/reduce-only flatten then owns the book).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from relay.execution.rounding import (
    format_price,
    format_qty,
    round_qty_to_step,
    round_stop_to_tick,
)

logger = logging.getLogger(__name__)


@dataclass
class SmartExecConfig:
    work_window_s: float = 1200.0        # ~20 min deadline to work maker before sweeping the residual
    chase_interval_s: float = 25.0       # poll/re-price cadence
    max_chase: int = 60                  # cap re-prices (don't chase a runaway forever)
    min_residual_frac: float = 0.02      # sweep only if unfilled > 2% of target (ignore dust)
    # Fail-closed on an UNREADABLE position read: don't work the leg / don't sweep a residual that
    # can't be sized. False = observe-only (log the unreadable read, behave as if it read 0) — kept
    # only so the failure rate can be measured before arming; the safe default is True.
    fail_closed: bool = True


@dataclass
class SmartExecDeps:
    place_limit: object     # async (symbol, bside, qty_str, price_str, sl_str|None, reduce_only) -> order_id|None
    amend_limit: object     # async (symbol, order_id, price_str) -> bool  (False if rejected e.g. would-cross)
    cancel: object          # async (symbol, order_id) -> bool|None  (False = non-benign failure, may still rest)
    touch: object           # async (symbol) -> (best_bid, best_ask)
    position: object        # async (symbol) -> {size, side, avg_price, has_stop, read_ok}
                            #   read_ok=False means the read FAILED. size/side/has_stop are then
                            #   MEANINGLESS — never treat it as flat, never size a sweep off it, and
                            #   never conclude 'naked'. A collaborator that cannot report readability
                            #   omits the key, which defaults to True.
    market_sweep: object    # async (symbol, bside, qty, sl_str|None) -> None   (taker residual, atomic SL)
    set_stop: object        # async (symbol, side, price) -> None               (re-anchor SL to avg fill)
    specs: object           # async (symbol) -> {qty_step, min_qty, tick_size}
    is_halted: object       # () -> bool  (guardian)
    sleep: object = None    # async (seconds) -> None       (injectable; defaults to asyncio.sleep)
    clock: object = None    # () -> float monotonic seconds (injectable; defaults to time.monotonic)

    def __post_init__(self):
        if self.sleep is None:
            import asyncio
            self.sleep = asyncio.sleep
        if self.clock is None:
            self.clock = time.monotonic


def _passive_side(side: str, reduce_only: bool) -> str:
    """Order side (Buy/Sell). Increase a long -> Buy; reduce a long (reduce_only) -> Sell; etc."""
    if reduce_only:
        return "Sell" if side == "long" else "Buy"
    return "Buy" if side == "long" else "Sell"


async def work_order(deps: SmartExecDeps, cfg: SmartExecConfig, symbol: str, side: str, qty: float,
                     arrival_price: float, stop_price: float | None, reduce_only: bool = False) -> dict:
    """Work ONE directional child (qty of `side`) as a post-only maker order, chasing the touch until
    filled or the deadline, then market-sweeping the residual. Returns telemetry (maker/taker split)."""
    if not qty or qty <= 0 or not arrival_price or arrival_price <= 0:
        return {"symbol": symbol, "target": 0.0, "maker": 0.0, "swept": 0.0, "reason": "noop"}
    specs = await deps.specs(symbol) or {}
    step = specs.get("qty_step"); tick = specs.get("tick_size")
    target = round_qty_to_step(qty, step, specs.get("min_qty"))
    if target <= 0:
        return {"symbol": symbol, "target": 0.0, "maker": 0.0, "swept": 0.0, "reason": "below_min"}

    bside = _passive_side(side, reduce_only)
    sl_str = format_price(stop_price, tick) if (stop_price and not reduce_only) else None

    state = {"filled": 0.0, "read_ok": True}

    async def _filled() -> float:
        pos = await deps.position(symbol) or {}
        state["read_ok"] = bool(pos.get("read_ok", True))
        if not state["read_ok"] and cfg.fail_closed:
            # UNKNOWN is not "zero filled" — hold the last known value. In observe-only mode this
            # falls through to the computation below (which reads an unreadable size as 0.0), so
            # observe-only behaves exactly like the unguarded path and only adds logging.
            return state["filled"]
        cur = abs(float(pos.get("size") or 0.0))
        # filled-of-this-order = signed change vs the size at start, on this order's direction
        state["filled"] = max(0.0, (cur - start_size) if not reduce_only else (start_size - cur))
        return state["filled"]

    pos0 = await deps.position(symbol) or {}
    if not pos0.get("read_ok", True):
        # `start_size` is the ONLY anchor for _filled(). A wrong 0.0 against a live position makes
        # _filled() report the ENTIRE pre-existing position as filled by THIS order: on the reduce
        # side (start_size - cur) goes negative -> clamped to 0 -> residual = the FULL target -> the
        # end-of-window sweep places a SECOND full-size MARKET order. That second order is the money
        # loss, and it is reachable even when every caller's pre-action reads are correct.
        logger.error("[smart-exec] %s: pre-trade position UNREADABLE — %s", symbol,
                     "NOT working this leg" if cfg.fail_closed else "working it anyway (observe-only)")
        if cfg.fail_closed:
            return {"symbol": symbol, "target": target, "maker": 0.0, "swept": 0.0,
                    "naked_repairs": 0, "reason": "position_unreadable"}
    start_size = abs(float(pos0.get("size") or 0.0))
    start_avg = float(pos0.get("avg_price") or 0.0)   # for THIS-cycle marginal fill vs blended avg (slip)

    # place the initial post-only child at the passive touch (buy@bid / sell@ask)
    bid, ask = await deps.touch(symbol)
    px = bid if bside == "Buy" else ask
    order_id = await deps.place_limit(symbol, bside, format_qty(target, step),
                                      format_price(px, tick), sl_str, reduce_only)

    t0 = deps.clock(); chases = 0; naked_repairs = 0
    # SL-under-fills: an entry leg must carry a native stop the instant it fills. The post-only child
    # AND the residual sweep both attach the SL atomically, so this only REPAIRS the rare case where the
    # atomic attach silently failed — checked once at the first fill; the end-of-window re-anchor is the
    # final guarantee. reduce-only / no-SL legs need no entry stop, so they start already 'ensured'.
    sl_ensured = reduce_only or not (stop_price and arrival_price)
    while deps.clock() - t0 < cfg.work_window_s:
        await deps.sleep(cfg.chase_interval_s)
        if deps.is_halted():                                   # guardian owns the book -> abort
            if order_id:
                await deps.cancel(symbol, order_id)
            return {"symbol": symbol, "target": target, "maker": await _filled(), "swept": 0.0,
                    "naked_repairs": naked_repairs, "reason": "guardian_abort"}
        filled = await _filled()
        if not sl_ensured and filled > 0:                      # first fill landed -> guarantee a stop
            pos = await deps.position(symbol) or {}
            # An UNREADABLE read reports has_stop=False from the binding, which satisfies the
            # "explicitly naked" test below and would fire a repair against unknown state. Skip and
            # retry on the next chase tick (sl_ensured stays False).
            _pos_ok = bool(pos.get("read_ok", True))
            if _pos_ok and pos.get("has_stop") is False:       # EXPLICITLY naked (not merely unknown)
                avg = float(pos.get("avg_price") or 0.0) or arrival_price
                dist = abs(arrival_price - stop_price) / arrival_price
                lvl = avg * (1 - dist) if side == "long" else avg * (1 + dist)
                await deps.set_stop(symbol, side, round_stop_to_tick(lvl, tick, side))
                naked_repairs += 1
                logger.warning("[smart-exec] %s filled but atomic SL missing -> attached stop (naked repair)", symbol)
            # Deliberately NOT gated on fail_closed: an unreadable read at the first fill must not
            # permanently retire the naked-SL check (that could leave a filled leg with no stop).
            # Retrying on the next chase tick is the mandatory-stop guarantee. It can only ever ADD
            # a stop to an explicitly-naked position on a READABLE read.
            sl_ensured = _pos_ok                               # unreadable -> retry on the next tick
        if filled + (step or 0.0) >= target:                   # fully filled as maker
            break
        bid, ask = await deps.touch(symbol)                    # chase the touch if it moved
        new_px = bid if bside == "Buy" else ask
        if abs(new_px - px) >= (tick or 1e-12) and chases < cfg.max_chase:
            ok = order_id and await deps.amend_limit(symbol, order_id, format_price(new_px, tick))
            if not ok:                                         # amend rejected (would cross) -> cancel+replace
                # Only replace if the cancel CONFIRMS the old child is gone. deps.cancel returns
                # False on a non-benign failure (the post-only may STILL be resting); placing a
                # replacement then would leave TWO live children -> double-fill/overshoot past the sized
                # notional. Fail closed: keep the old resting order + price, skip this re-price tick.
                cancelled = (await deps.cancel(symbol, order_id)) if order_id else True
                if cancelled:
                    remaining = round_qty_to_step(target - filled, step, None)
                    order_id = (await deps.place_limit(symbol, bside, format_qty(remaining, step),
                                                       format_price(new_px, tick), sl_str, reduce_only)
                                if remaining > 0 else None)
                    px = new_px
                else:
                    logger.error("[smart-exec] %s: cancel failed — NOT replacing (avoid double resting order)", symbol)
                chases += 1
            else:
                px = new_px; chases += 1

    cancelled = True
    if order_id:
        cancelled = await deps.cancel(symbol, order_id)        # cancel any resting child before the sweep
    filled = await _filled()
    if not state["read_ok"]:
        # SECOND DOUBLING PATH. An unreadable final read would leave `filled` at 0, so `residual`
        # becomes the FULL target and the block below fires a full-size MARKET order on top of a
        # maker child that may already have filled it. The residual cannot be sized without a true
        # fill count; leave the leg for the next cycle instead of guessing.
        logger.error("[smart-exec] %s: end-of-window position UNREADABLE — %s (last known fill %.6g)",
                     symbol, "SKIPPING the residual sweep" if cfg.fail_closed
                     else "sweeping anyway (observe-only)", filled)
        if cfg.fail_closed:
            return {"symbol": symbol, "target": target, "maker": filled, "swept": 0.0, "chases": chases,
                    "maker_rate": (filled / target if target else 0.0), "avg_fill": 0.0,
                    "slippage_bps": 0.0, "naked_repairs": naked_repairs,
                    "reason": "position_unreadable_final"}
    residual = round_qty_to_step(target - filled, step, specs.get("min_qty"))
    swept = 0.0
    if cancelled is False:
        # `is False` (not falsy): deps.cancel returns STRICTLY True/False, and False is the explicit
        # "non-benign failure, the child may still be resting" signal. A None from a collaborator that
        # doesn't report is not that signal and must not suppress the sweep.
        # Same rule as the mid-loop replace path above: if the post-only child MAY STILL BE RESTING,
        # market-sweeping the same residual leaves BOTH live and the exchange ends at up to 2x the
        # target while the books record 1x (a 2.00x size multiple was observed in production before
        # this guard). Skip the sweep and report it so the caller does NOT treat this leg as settled
        # (an exit must not close its DB row on this path).
        logger.error("[smart-exec] %s: end-of-window cancel FAILED — child may still rest; SKIPPING "
                     "the residual sweep to avoid a double-fill (residual %.6g)", symbol, residual)
        return {"symbol": symbol, "target": target, "maker": filled, "swept": 0.0, "chases": chases,
                "maker_rate": (filled / target if target else 0.0), "avg_fill": 0.0,
                "slippage_bps": 0.0, "naked_repairs": naked_repairs, "reason": "cancel_failed"}
    if residual > 0 and residual >= target * cfg.min_residual_frac:
        await deps.market_sweep(symbol, bside, residual, sl_str)   # taker residual, atomic SL
        swept = residual

    maker = filled                              # position delta measured BEFORE the sweep = passive fill
    # final position (one fetch): re-anchor the SL to the actual avg fill (preserving the requested
    # stop DISTANCE) AND read the avg for slippage telemetry. Not re-anchored for reduce_only
    # (that closes; any remaining position keeps its own stop).
    pos = await deps.position(symbol) or {}
    avg = float(pos.get("avg_price") or 0.0)
    end_size = abs(float(pos.get("size") or 0.0))
    if not reduce_only and stop_price and arrival_price and avg > 0:
        dist = abs(arrival_price - stop_price) / arrival_price
        final = avg * (1 - dist) if side == "long" else avg * (1 + dist)
        await deps.set_stop(symbol, side, round_stop_to_tick(final, tick, side))

    # Slippage telemetry: SIGNED slippage vs arrival (+ = adverse: bought higher / sold lower).
    # Measure THIS cycle's marginal fill price, NOT the blended position avg: on a resize/add the
    # avg is dominated by OLD fills, so slip-vs-avg reports the position's unrealized P&L (±100s of bps),
    # not execution quality. Marginal VWAP = (avg1·size1 − avg0·size0)/(size1−size0); for a fresh enter
    # (start_size 0) it reduces to avg1. Fall back to avg if the delta is degenerate (reduce-only/no add).
    added = end_size - start_size
    fill_px = ((avg * end_size - start_avg * start_size) / added
               if (not reduce_only and added > 1e-12 and avg > 0 and start_avg > 0) else avg)
    sign = 1.0 if side == "long" else -1.0
    slip_bps = ((fill_px - arrival_price) / arrival_price * 1e4 * sign) if (fill_px > 0 and arrival_price) else 0.0
    maker_rate = maker / target if target else 0.0
    logger.info("[smart-exec] %s %s target %.6g maker %.6g (%.0f%%) swept %.6g chases %d slip %+.1fbps naked_fix %d",
                symbol, bside, target, maker, 100 * maker_rate, swept, chases, slip_bps, naked_repairs)
    return {"symbol": symbol, "target": target, "maker": maker, "swept": swept, "chases": chases,
            "maker_rate": maker_rate, "avg_fill": avg, "slippage_bps": slip_bps, "naked_repairs": naked_repairs}


async def work_book(orders: list[dict], deps: SmartExecDeps, cfg: SmartExecConfig) -> list[dict]:
    """Work all legs of a book concurrently. Each item: {symbol, side, qty, arrival_price,
    stop_price, reduce_only}. Returns per-leg telemetry; logs the aggregate maker-fill rate."""
    import asyncio
    results = await asyncio.gather(*(
        work_order(deps, cfg, o["symbol"], o["side"], o["qty"], o["arrival_price"],
                   o.get("stop_price"), o.get("reduce_only", False))
        for o in orders
    ), return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    tgt = sum(r["target"] for r in out) or 1.0
    maker_rate = sum(r["maker"] for r in out) / tgt
    avg_slip = sum(r.get("slippage_bps", 0.0) * r["target"] for r in out) / tgt   # notional-weighted
    naked = sum(r.get("naked_repairs", 0) for r in out)
    logger.info("[smart-exec] book done: %d legs, maker-fill %.1f%%, notional-wtd slip %+.1fbps, "
                "%d swept residuals, %d naked-repairs", len(out), 100 * maker_rate, avg_slip,
                sum(1 for r in out if r["swept"] > 0), naked)
    return out
