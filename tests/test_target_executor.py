"""Target executor: a v1 target signal carried out on the engine, leg by leg, margin first."""
import asyncio
import uuid

import pytest

from relay.signals.contract import parse_signal
from relay.signals.log import SignalLog
from relay.signals.target_executor import TargetExecutor
from tests.test_engine import SETTINGS, FakeBybit, _engine, _no_sleep, _rows  # noqa: F401

ENV = {"version": "1", "source": "quant", "deployment": "default", "sent_at": "2026-11-02T08:00:00Z",
       "kind": "target"}
BTC, ETH, SOL = "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"
OPEN_SETTINGS = {**SETTINGS, "max_positions": None, "max_exposure_pct": None}


async def _eng(db, fake=None, dry_run=True, **settings):
    eng, _ = await _engine(db, fake or FakeBybit(), dry_run=dry_run, settings={**OPEN_SETTINGS, **settings})
    eng.signal_log = SignalLog(db)
    return eng


async def _run(eng, weights, **kw):
    body = {**ENV, "id": uuid.uuid4().hex, "weights": weights, "stop_pct": 2, **kw}
    sig = parse_signal(body)
    row, _ = await eng.signal_log.record(sig.source, sig.id, body)
    res = await TargetExecutor(eng, eng.signal_log).execute(sig, row.id)
    return res, await eng.signal_log.get(row.id)


async def _book(db):
    rows = await _rows(db, "SELECT symbol, side, size, status, dry_run, initial_stop_price FROM positions "
                           "WHERE status = 'OPEN' ORDER BY symbol")
    return {r["symbol"]: (r["side"], r["size"]) for r in rows}


async def test_an_empty_paper_book_is_built_with_stops_and_nothing_sent(db):
    fake = FakeBybit()
    eng = await _eng(db, fake)
    res, row = await _run(eng, {BTC: 0.05, ETH: -0.03})               # 500 / 300 of 10000 at 100
    assert res["success"] and row.status == "executed" and res["dry_run"] is True
    assert await _book(db) == {"BTCUSDT": ("LONG", 5.0), "ETHUSDT": ("SHORT", 3.0)}
    stops = await _rows(db, "SELECT symbol, initial_stop_price FROM positions ORDER BY symbol")
    assert [(r["symbol"], r["initial_stop_price"]) for r in stops] == [("BTCUSDT", 98.0), ("ETHUSDT", 102.0)]
    assert not {"place_market_order", "set_trading_stop"} & set(fake.names())


async def test_a_rebalance_resizes_exits_and_enters(db):
    eng = await _eng(db)
    await _run(eng, {BTC: 0.05, ETH: -0.03})
    res, _ = await _run(eng, {BTC: 0.08, SOL: 0.02})                  # BTC +3, ETH out, SOL in
    assert res["success"], res
    assert [(lg["symbol"], lg["action"]) for lg in res["legs"]] == [(ETH, "exit"), (BTC, "resize"), (SOL, "enter")]
    assert await _book(db) == {"BTCUSDT": ("LONG", 8.0), "SOLUSDT": ("LONG", 2.0)}


async def test_a_smaller_target_reduces_and_another_side_flips(db):
    eng = await _eng(db)
    await _run(eng, {BTC: 0.05, ETH: -0.03})
    res, _ = await _run(eng, {BTC: 0.02, ETH: 0.03})
    assert res["success"], res
    assert await _book(db) == {"BTCUSDT": ("LONG", 2.0), "ETHUSDT": ("LONG", 3.0)}


async def test_changes_under_the_users_threshold_are_held(db):
    fake = FakeBybit()
    eng = await _eng(db, fake, target={"neutralize": False, "min_change_pct": 1})   # 100 of 10000
    await _run(eng, {BTC: 0.05})
    fake.calls.clear()
    res, row = await _run(eng, {BTC: 0.055})                          # +50: held
    assert res["held"] == [BTC] and res["legs"] == [] and row.status == "executed"
    assert await _book(db) == {"BTCUSDT": ("LONG", 5.0)}


async def test_the_neutralize_option_and_the_gross_rail(db):
    eng = await _eng(db, target={"neutralize": True, "min_change_pct": None},
                     rails={"symbols": {"allow": [], "deny": []}, "max_gross_leverage": 0.04})
    res, _ = await _run(eng, {BTC: 0.06, ETH: -0.03})   # 600/300, gross 900 -> 400: 266.7/133.3; neutral 133.3 each
    assert res["success"], res
    assert await _book(db) == {"BTCUSDT": ("LONG", 1.33), "ETHUSDT": ("SHORT", 1.33)}
    assert any("scaled" in n for n in res["notes"])


async def test_a_live_book_out_of_sync_with_the_venue_trades_nothing(db):
    fake = FakeBybit()
    eng = await _eng(db, fake, dry_run=False)
    await _run(eng, {BTC: 0.05})
    fake.calls.clear()
    fake.positions = []                                               # the venue shows nothing
    res, row = await _run(eng, {BTC: 0.08})
    assert (res["reason"], row.status) == ("book_out_of_sync", "skipped")
    assert "place_market_order" not in fake.names()
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100"}]
    res, _ = await _run(eng, {BTC: 0.08})
    assert res["success"] and await _book(db) == {"BTCUSDT": ("LONG", 8.0)}


async def test_a_book_with_a_close_in_progress_trades_nothing(db):
    eng = await _eng(db)
    await _run(eng, {BTC: 0.05})
    (p,) = await _rows(db, "SELECT id FROM positions")
    eng._own_close(p["id"])
    res, _ = await _run(eng, {ETH: 0.05})
    assert res["reason"] == "book_unsettled"


async def test_a_failed_close_stops_the_new_exposure(db, monkeypatch):
    eng = await _eng(db)
    await _run(eng, {BTC: 0.05})

    async def broken(position, reason="manual", client=None):
        raise Exception("Failed to close position BTCUSDT: venue down")
    monkeypatch.setattr(eng, "execute_exit", broken)
    res, row = await _run(eng, {ETH: 0.05})                           # BTC out, ETH in
    assert row.status == "failed"
    by = {lg["symbol"]: lg for lg in res["legs"]}
    assert by[BTC]["reason"] == "close_failed" and by[ETH]["reason"] == "not_attempted"
    assert await _book(db) == {"BTCUSDT": ("LONG", 5.0)}


async def test_a_leg_the_engine_refuses_is_reported_and_the_rest_goes_on(db):
    eng = await _eng(db, rails={"symbols": {"allow": [], "deny": []}})
    res, row = await _run(eng, {BTC: 0.05, ETH: 0.000001})            # ETH too small: dropped as a note
    assert res["success"] and any("ETH" in n for n in res["notes"])
    eng._settings_provider = {**OPEN_SETTINGS, "enabled": False}
    res, row = await _run(eng, {SOL: 0.05})                           # BTC exits; SOL refused
    by = {lg["symbol"]: lg for lg in res["legs"]}
    assert by[BTC]["success"] and by[SOL]["reason"] == "deployment_disabled" and row.status == "failed"
    assert await _book(db) == {}


async def test_a_dry_run_signal_never_touches_the_live_book(db):
    fake = FakeBybit()
    eng = await _eng(db, fake, dry_run=False)
    await _run(eng, {BTC: 0.05})                                      # live BTC
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100"}]
    fake.calls.clear()
    res, _ = await _run(eng, {ETH: 0.03}, dry_run=True)               # paper book: ETH in, nothing out
    assert res["success"] and res["dry_run"] is True
    assert [(lg["symbol"], lg["action"]) for lg in res["legs"]] == [(ETH, "enter")]
    assert "place_market_order" not in fake.names()
    rows = await _rows(db, "SELECT symbol, dry_run FROM positions WHERE status = 'OPEN' ORDER BY symbol")
    assert [(r["symbol"], bool(r["dry_run"])) for r in rows] == [("BTCUSDT", False), ("ETHUSDT", True)]


async def test_target_legs_carry_their_stop_only(db):
    eng = await _eng(db, take_profit_ladder=[{"pct": 2, "close_pct": 50}], trailing_distance_pct=1.0,
                     break_even_trigger_pct=1.0)
    res, _ = await _run(eng, {BTC: 0.05})
    (p,) = await _rows(db, "SELECT management, initial_stop_price FROM positions")
    assert res["success"] and p["management"] is None and p["initial_stop_price"] == 98.0


@pytest.mark.parametrize("weights, extra, reason", [
    ({"ETH/USDC:USDC": 0.05}, {}, "unsupported_symbol"),
    ({BTC: 0.05}, {"execution": {"type": "maker_chase", "window_s": 60}}, "execution_not_supported"),
])
async def test_signals_the_relay_cannot_carry_out_yet(db, weights, extra, reason):
    eng = await _eng(db)
    res, row = await _run(eng, weights, **extra)
    assert (res["reason"], row.status) == (reason, "skipped") and await _book(db) == {}


async def test_one_target_signal_at_a_time(db, monkeypatch):
    eng = await _eng(db)
    ex = TargetExecutor(eng, eng.signal_log)
    running, peak = 0, 0

    async def slow(sig):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        for _ in range(10):
            await asyncio.sleep(0)
        running -= 1
        return {"success": True}
    monkeypatch.setattr(ex, "_execute", slow)
    sigs = [parse_signal({**ENV, "id": f"s{i}", "weights": {BTC: 0.01}}) for i in range(3)]
    rows = [(await eng.signal_log.record("quant", s.id, {}))[0] for s in sigs]
    await asyncio.gather(*(ex.execute(s, r.id) for s, r in zip(sigs, rows, strict=True)))
    assert peak == 1


async def test_the_venue_holding_the_other_side_is_out_of_sync(db):
    fake = FakeBybit()
    eng = await _eng(db, fake, dry_run=False)
    await _run(eng, {BTC: 0.05})
    fake.positions = [{"symbol": "BTCUSDT", "side": "Sell", "size": "5", "avgPrice": "100"}]   # same size, short
    res, _ = await _run(eng, {BTC: 0.08})
    assert res["reason"] == "book_out_of_sync"


async def test_the_signals_gross_leverage_scales_the_book(db):
    eng = await _eng(db)
    res, _ = await _run(eng, {BTC: 0.05}, gross_leverage=2)           # 1000, not 500
    assert res["success"] and await _book(db) == {"BTCUSDT": ("LONG", 10.0)}


# ---- review round (R3) -------------------------------------------------------------------------

async def test_a_change_too_small_for_the_venue_is_held_not_failed(db):
    eng = await _eng(db)
    await _run(eng, {BTC: 0.05, ETH: 0.05})
    res, row = await _run(eng, {BTC: 0.05005, ETH: 0.05, SOL: 0.02})   # BTC +0.5 USD = 0.005 < 0.01 lot
    assert res["success"] and BTC in res["held"] and row.status == "executed"
    assert await _book(db) == {"BTCUSDT": ("LONG", 5.0), "ETHUSDT": ("LONG", 5.0), "SOLUSDT": ("LONG", 2.0)}


async def test_after_a_failed_close_a_flip_only_closes(db, monkeypatch):
    eng = await _eng(db)
    await _run(eng, {BTC: 0.05, ETH: 0.05})
    real = eng.execute_exit

    async def btc_fails(position, reason="manual", client=None):
        if position["symbol"] == "BTCUSDT":
            raise Exception("Failed to close position BTCUSDT: venue down")
        return await real(position, reason=reason, client=client)
    monkeypatch.setattr(eng, "execute_exit", btc_fails)
    res, row = await _run(eng, {ETH: -0.05})                          # BTC exit fails; ETH flip
    by = {lg["symbol"]: lg for lg in res["legs"]}
    assert by[BTC]["reason"] == "close_failed" and by[ETH]["reason"] == "not_attempted"
    assert await _book(db) == {"BTCUSDT": ("LONG", 5.0)}               # ETH closed, no new short


async def test_an_unknown_venue_position_on_a_target_symbol_stops_the_book(db):
    fake = FakeBybit()
    eng = await _eng(db, fake, dry_run=False)
    fake.positions = [{"symbol": "BTCUSDT", "side": "Sell", "size": "1", "avgPrice": "100"}]   # not the relay's
    res, _ = await _run(eng, {BTC: 0.05})
    assert res["reason"] == "book_out_of_sync" and "place_market_order" not in fake.names()


async def test_another_live_record_on_a_target_symbol_stops_the_book(db):
    from datetime import UTC, datetime
    fake = FakeBybit()
    eng = await _eng(db, fake, dry_run=False)
    async with db.acquire() as c:                                    # e.g. imported from outside the relay
        await c.execute("INSERT INTO positions (symbol, side, size, entry_price, status, dry_run, opened_at, "
                        "created_at, updated_at) VALUES ('ETHUSDT', 'LONG', 5, 100, 'OPEN', false, $1, $1, $1)",
                        datetime.now(UTC))
    fake.positions = [{"symbol": "ETHUSDT", "side": "Buy", "size": "5", "avgPrice": "100"}]
    res, _ = await _run(eng, {ETH: 0.05})
    assert res["reason"] == "shared_venue_position"


async def test_a_book_without_a_stop_is_refused_when_one_is_required(db):
    fake = FakeBybit()
    eng = await _eng(db, fake)
    await _run(eng, {BTC: 0.05})
    body = {**ENV, "id": "nostop", "weights": {ETH: -0.05}}
    sig = parse_signal(body)
    row, _ = await eng.signal_log.record(sig.source, sig.id, body)
    res = await TargetExecutor(eng, eng.signal_log).execute(sig, row.id)
    assert res["reason"] == "stop_required" and await _book(db) == {"BTCUSDT": ("LONG", 5.0)}


async def test_a_book_the_venue_would_refuse_is_refused_before_anything_is_closed(db):
    fake = FakeBybit()
    eng = await _eng(db, fake)
    await _run(eng, {"BTCUSDT": 0.05})
    before = await _book(db)
    fake.can_short = False
    res, row = await _run(eng, {"BTCUSDT": 0.05, "ETHUSDT": -0.03})
    assert (res["reason"], row.status) == ("short_unsupported", "skipped") and await _book(db) == before
    fake.can_short, fake.can_stop = True, False
    res, row = await _run(eng, {"ETHUSDT": 0.03})
    assert (res["reason"], row.status) == ("stop_unsupported", "skipped") and await _book(db) == before
