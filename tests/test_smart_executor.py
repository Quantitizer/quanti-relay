"""SmartExecutor core — maker-chase, guaranteed residual sweep, SL re-anchor, guardian abort.
All collaborators + clock/sleep are injected, so no live exchange is touched."""
import asyncio

import pytest

from relay.execution.smart_executor import SmartExecConfig, SmartExecDeps, work_book, work_order


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _mk(fill_to, fill_time, halted=False, avg=110.0, touch_fn=None, has_stop=None):
    """Harness: the position fills linearly to `fill_to` over `fill_time` seconds (clock-driven).
    has_stop: None = position does not report stop status (the naked-guard can't tell -> no-op);
    False/True = explicitly report whether a native SL is attached (drives the naked-repair path)."""
    clk = _Clock()
    calls = {"place": [], "amend": [], "cancel": [], "sweep": [], "set_stop": []}

    async def sleep(s):
        clk.t += s

    async def specs(sym):
        return {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.1}

    async def touch(sym):
        return touch_fn(clk) if touch_fn else (100.0, 100.2)   # (bid, ask)

    async def position(sym):
        prog = min(1.0, clk.t / fill_time) if fill_time > 0 else 1.0
        d = {"size": fill_to * prog, "side": "long", "avg_price": avg}
        if has_stop is not None:
            d["has_stop"] = has_stop
        return d

    async def place_limit(sym, bside, qty, price, sl, ro):
        calls["place"].append((bside, qty, price, sl, ro)); return "oid"

    async def amend_limit(sym, oid, price):
        calls["amend"].append(price); return True

    async def cancel(sym, oid):
        calls["cancel"].append(oid)

    async def market_sweep(sym, bside, qty, sl):
        calls["sweep"].append((bside, round(qty, 6), sl))

    async def set_stop(sym, side, price):
        calls["set_stop"].append(round(price, 4))

    deps = SmartExecDeps(place_limit=place_limit, amend_limit=amend_limit, cancel=cancel, touch=touch,
                         position=position, market_sweep=market_sweep, set_stop=set_stop, specs=specs,
                         is_halted=lambda: halted, sleep=sleep, clock=clk)
    return deps, calls


def test_failed_cancel_does_not_double_order():
    # amend rejected -> cancel FAILS (order may still rest) -> must NOT place a replacement
    # (a replace over a still-resting post-only = two live children = double-fill / overshoot).
    clk = _Clock()
    calls = {"place": [], "cancel": []}

    async def sleep(s): clk.t += s
    async def specs(sym): return {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.1}
    seq = [(100.0, 100.2), (105.0, 105.2), (110.0, 110.2), (115.0, 115.2)]
    idx = {"i": 0}

    async def touch(sym):
        i = min(idx["i"], len(seq) - 1); idx["i"] += 1
        return seq[i]                                          # price keeps moving -> forces amend/replace

    async def position(sym):
        return {"size": 0.0, "side": "long", "avg_price": 0.0}  # never fills -> keeps chasing

    async def place_limit(sym, bside, qty, price, sl, ro):
        calls["place"].append(price); return "oid"

    async def amend_limit(sym, oid, price): return False       # always reject -> cancel+replace path
    async def cancel(sym, oid): calls["cancel"].append(oid); return False   # cancel FAILS
    async def market_sweep(sym, bside, qty, sl): pass
    async def set_stop(sym, side, price): pass

    deps = SmartExecDeps(place_limit=place_limit, amend_limit=amend_limit, cancel=cancel, touch=touch,
                         position=position, market_sweep=market_sweep, set_stop=set_stop, specs=specs,
                         is_halted=lambda: False, sleep=sleep, clock=clk)
    asyncio.run(work_order(deps, SmartExecConfig(work_window_s=5.0, chase_interval_s=1.0),
                           "BTCUSDT", "long", 10.0, 100.0, 92.0))
    assert len(calls["place"]) == 1, f"double order! a failed cancel led to a replacement: {calls['place']}"
    assert calls["cancel"], "cancel should have been attempted on the amend reject"


def test_full_maker_fill_no_sweep():
    deps, calls = _mk(fill_to=10.0, fill_time=100.0)              # fully fills at t=100 << window
    r = asyncio.run(work_order(deps, SmartExecConfig(), "BTCUSDT", "long", 10.0, 100.0, 92.0))
    assert calls["place"] and calls["place"][0][0] == "Buy"      # post-only buy at the bid
    assert calls["sweep"] == []                                  # NO residual sweep -> 100% maker
    assert r["maker"] == pytest.approx(10.0, abs=0.01) and r["swept"] == 0.0
    assert calls["set_stop"] == [pytest.approx(101.2)]            # SL re-anchored 8% below the 110 fill


def test_partial_fill_sweeps_residual():
    deps, calls = _mk(fill_to=6.0, fill_time=50.0)               # only 6 of 10 fill passively
    r = asyncio.run(work_order(deps, SmartExecConfig(work_window_s=200.0), "BTCUSDT", "long",
                               10.0, 100.0, 92.0))
    assert calls["sweep"] and calls["sweep"][0][0] == "Buy"      # residual swept to market
    assert calls["sweep"][0][1] == pytest.approx(4.0, abs=0.05)  # ~4 residual
    assert r["swept"] == pytest.approx(4.0, abs=0.05) and r["maker"] == pytest.approx(6.0, abs=0.05)


def test_guardian_halt_aborts_without_sweep():
    deps, calls = _mk(fill_to=10.0, fill_time=100.0, halted=True)
    r = asyncio.run(work_order(deps, SmartExecConfig(), "BTCUSDT", "long", 10.0, 100.0, 92.0))
    assert r["reason"] == "guardian_abort"
    assert calls["sweep"] == [] and calls["cancel"]              # never sweeps on abort; cancels the child


def test_chase_reprices_toward_moving_touch():
    def moving(clk):
        return (100.0 + clk.t * 0.01, 100.2 + clk.t * 0.01)     # bid drifts up over time
    deps, calls = _mk(fill_to=0.0, fill_time=1.0, touch_fn=moving)   # never fills passively -> chases
    asyncio.run(work_order(deps, SmartExecConfig(work_window_s=200.0, chase_interval_s=25.0),
                           "BTCUSDT", "long", 10.0, 100.0, 92.0))
    assert calls["amend"]                                        # re-priced the resting maker order
    assert calls["sweep"] and calls["sweep"][0][1] == pytest.approx(10.0, abs=0.05)  # full sweep at deadline


def test_reduce_only_no_stop_attached():
    deps, calls = _mk(fill_to=10.0, fill_time=100.0)
    asyncio.run(work_order(deps, SmartExecConfig(), "BTCUSDT", "long", 10.0, 100.0, None, reduce_only=True))
    assert calls["place"][0][0] == "Sell" and calls["place"][0][3] is None   # sell to reduce, no SL
    assert calls["set_stop"] == []                              # reduce never re-stops


def test_work_book_aggregates():
    deps, calls = _mk(fill_to=10.0, fill_time=100.0)
    orders = [{"symbol": "BTCUSDT", "side": "long", "qty": 10.0, "arrival_price": 100.0, "stop_price": 92.0}]
    out = asyncio.run(work_book(orders, deps, SmartExecConfig()))
    assert len(out) == 1 and out[0]["maker"] == pytest.approx(10.0, abs=0.01)


# ---- SL-under-fills repair + slippage telemetry ----
def test_naked_fill_attaches_stop_when_atomic_sl_missing():
    # The atomic SL on the child silently failed (position reports has_stop=False) -> the moment a fill
    # lands, work_order must attach the fill-anchored stop rather than sit naked until the deadline.
    deps, calls = _mk(fill_to=10.0, fill_time=50.0, has_stop=False)
    r = asyncio.run(work_order(deps, SmartExecConfig(work_window_s=300.0), "BTCUSDT", "long",
                               10.0, 100.0, 92.0))
    assert r["naked_repairs"] == 1
    assert 101.2 in calls["set_stop"]                 # mid-window repair = 8% below the 110 fill


def test_no_naked_repair_when_stop_status_unknown():
    # Position doesn't report stop status (has_stop absent) -> the atomic SL is assumed good; NO extra
    # mid-window set_stop, only the single end-of-window re-anchor (regression guard for existing paths).
    deps, calls = _mk(fill_to=10.0, fill_time=100.0)
    r = asyncio.run(work_order(deps, SmartExecConfig(), "BTCUSDT", "long", 10.0, 100.0, 92.0))
    assert r["naked_repairs"] == 0 and calls["set_stop"] == [pytest.approx(101.2)]


def test_slip_measures_marginal_fill_not_blended_avg_on_resize():
    """Regression: on a resize/add, slip must reflect THIS cycle's marginal fill vs arrival, NOT the
    blended position avg (which is dominated by OLD fills and reports position P&L, not execution)."""
    clk = _Clock()

    async def sleep(s):
        clk.t += s

    async def specs(sym):
        return {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.1}

    async def touch(sym):
        return (121.0, 121.2)

    async def position(sym):
        # Existing long 10 @ 100; this cycle adds 10 more. Marginal fills land at 121 (= arrival, ~0 slip).
        prog = min(1.0, clk.t / 10.0)
        size = 10.0 + 10.0 * prog
        avg = 100.0 if prog < 1.0 else 110.5     # blended avg after adding 10 @ ~121: (1000+1210)/20
        return {"size": size, "side": "long", "avg_price": avg}

    async def _ok(*a, **k):
        return "oid"

    async def _noop(*a, **k):
        return None

    deps = SmartExecDeps(place_limit=_ok, amend_limit=lambda *a: _ok(), cancel=_noop, touch=touch,
                         position=position, market_sweep=_noop, set_stop=_noop, specs=specs,
                         is_halted=lambda: False, sleep=sleep, clock=clk)
    cfg = SmartExecConfig(work_window_s=30, chase_interval_s=5)
    r = asyncio.run(work_order(deps, cfg, "X", "long", 10.0, arrival_price=121.0, stop_price=None))
    # marginal fill = (110.5·20 − 100·10)/10 = 121 = arrival -> ~0 slip.
    # blended-avg slip (the OLD bug) would be (110.5−121)/121 ≈ -868 bps.
    assert abs(r["slippage_bps"]) < 5.0


def test_telemetry_reports_signed_slippage_and_maker_rate():
    deps, calls = _mk(fill_to=10.0, fill_time=100.0, avg=110.0)   # long filled at 110 vs arrival 100
    r = asyncio.run(work_order(deps, SmartExecConfig(), "BTCUSDT", "long", 10.0, 100.0, 92.0))
    assert r["slippage_bps"] == pytest.approx(1000.0, abs=1)      # +10% = adverse for a long buy
    assert r["maker_rate"] == pytest.approx(1.0, abs=0.01) and r["avg_fill"] == pytest.approx(110.0)


# ============================================================================================
# FAIL-CLOSED POSITION READS — the SECOND doubling path.
#
# The caller's pre-action reads are only half of it. `work_order` reads the position three more
# times and, pre-fix, checked `read_ok` in none of them. The dangerous one is the end-of-window
# read: an unreadable answer left `filled` at 0, so `residual` became the FULL target and a
# full-size MARKET order fired on top of a maker child that may already have filled it.
# ============================================================================================

def _mk_unreadable(readable_until=None, fill_to=1.0, fill_time=100.0, fail_closed=True):
    """Position reads succeed while `clk.t < readable_until`, then start failing (read_ok=False).
    `readable_until=None` means every read fails, including the pre-trade prefetch."""
    clk = _Clock()
    calls = {"place": [], "amend": [], "cancel": [], "sweep": [], "set_stop": []}

    async def sleep(s):
        clk.t += s

    async def specs(sym):
        return {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.1}

    async def touch(sym):
        return (100.0, 100.2)

    async def position(sym):
        if readable_until is None or clk.t >= readable_until:
            # the binding's failure shape: has_stop is None (neither attached nor explicitly naked)
            return {"size": 0.0, "side": None, "has_stop": None, "stop_price": None,
                    "avg_price": 0.0, "read_ok": False}
        prog = min(1.0, clk.t / fill_time) if fill_time > 0 else 1.0
        return {"size": fill_to * prog, "side": "long", "avg_price": 110.0,
                "has_stop": False, "read_ok": True}

    async def place_limit(sym, bside, qty, price, sl, ro):
        calls["place"].append((bside, qty, price, sl, ro)); return "oid"

    async def amend_limit(sym, oid, price):
        calls["amend"].append(price); return True

    async def cancel(sym, oid):
        calls["cancel"].append(oid); return True

    async def market_sweep(sym, bside, qty, sl):
        calls["sweep"].append((bside, round(qty, 6), sl))

    async def set_stop(sym, side, price):
        calls["set_stop"].append(round(price, 4))

    deps = SmartExecDeps(place_limit=place_limit, amend_limit=amend_limit, cancel=cancel, touch=touch,
                         position=position, market_sweep=market_sweep, set_stop=set_stop, specs=specs,
                         is_halted=lambda: False, sleep=sleep, clock=clk)
    return deps, calls, SmartExecConfig(work_window_s=100.0, chase_interval_s=25.0,
                                        fail_closed=fail_closed)


def test_work_order_aborts_when_prefetch_position_unreadable():
    """`start_size` is the ONLY anchor for _filled(). A wrong 0.0 against a live position makes
    _filled() report the entire pre-existing position as filled by THIS order. Place nothing."""
    deps, calls, cfg = _mk_unreadable(readable_until=None)
    tele = asyncio.run(work_order(deps, cfg, "BTCUSDT", "long", 1.0, 100.0, 92.0))
    assert calls["place"] == [] and calls["sweep"] == []
    assert tele["reason"] == "position_unreadable"


def test_work_order_skips_sweep_when_final_read_unreadable():
    """THE SECOND DOUBLING PATH. The maker child fills, then the end-of-window read fails. Pre-fix
    that read as 'nothing filled', so the residual was the FULL target and a full-size MARKET order
    went out on top of the fill. The residual cannot be sized without a true fill count."""
    deps, calls, cfg = _mk_unreadable(readable_until=60.0, fill_to=0.5, fill_time=100.0)
    tele = asyncio.run(work_order(deps, cfg, "BTCUSDT", "long", 1.0, 100.0, 92.0))
    assert calls["place"], "the maker child must still have been worked"
    assert calls["sweep"] == [], "must not market-sweep a residual it cannot size"
    assert tele["reason"] == "position_unreadable_final"
    assert tele["maker"] > 0, "the last KNOWN fill is held, not reset to zero"


def test_observe_only_mode_still_sweeps_the_full_target():
    """Observe-only inertness check, and the executable statement of the bug: unarmed, the same
    unreadable final read sweeps the FULL 1.0 target as a market order even though the maker child
    had already filled part of it. If this ever stops reproducing, observe-only mode is not neutral."""
    deps, calls, cfg = _mk_unreadable(readable_until=60.0, fill_to=0.5, fill_time=100.0,
                                      fail_closed=False)
    asyncio.run(work_order(deps, cfg, "BTCUSDT", "long", 1.0, 100.0, 92.0))
    assert calls["sweep"] == [("Buy", 1.0, "92.0")], \
        "pre-fix behaviour: full-size sweep on top of a fill"


def test_no_naked_repair_when_position_unreadable():
    """The binding returns has_stop=None (not False) on a failed read, and the repair now also gates
    on read_ok — so an unreadable position can never be mistaken for an EXPLICITLY naked one."""
    deps, calls, cfg = _mk_unreadable(readable_until=None, fail_closed=False)
    asyncio.run(work_order(deps, cfg, "BTCUSDT", "long", 1.0, 100.0, 92.0))
    assert calls["set_stop"] == []
