"""Adding to an open position (ExecutionEngine.execute_increase): size, weighted entry, PnL, checks."""
import pytest

from tests.test_engine import SETTINGS, FakeBybit, _engine, _no_sleep, _rows, _sig  # noqa: F401


async def _open(eng, **kw):
    r = await eng.execute_entry(_sig(**kw))
    assert r["success"], r
    return {"id": r["position_id"]}


async def _row(db, pid):
    (r,) = await _rows(db, "SELECT size, entry_price, status, pnl, pnl_source, entry_meta FROM positions "
                           "WHERE id = $1", pid)
    return r


async def _adds(db, pid):
    return await _rows(db, "SELECT qty, price, side, order_id FROM trades WHERE position_id = $1 "
                           "AND trade_type = 'entry' ORDER BY id", pid)


async def test_a_paper_add_grows_the_size_at_the_weighted_entry_and_sends_nothing(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)                                             # 5 @ 100
    fake.price = 110.0
    fake.calls.clear()
    r = await eng.execute_increase(pos, 5.0)
    assert r["success"] and (r["size"], r["entry_price"], r["fill_price"]) == (10.0, 105.0, 110.0)
    assert "place_market_order" not in fake.names() and "set_trading_stop" not in fake.names()
    row = await _row(db, pos["id"])
    assert (row["size"], row["entry_price"]) == (10.0, 105.0)
    adds = await _adds(db, pos["id"])
    assert [(a["qty"], a["price"], a["order_id"]) for a in adds][-1] == (5.0, 110.0, None)


async def test_a_live_add_is_one_market_order_and_the_whole_position_stop_stays(db):
    fake = FakeBybit()
    eng, dep = await _engine(db, fake, dry_run=False)
    pos = await _open(eng, direction="short", stop_price=105.0)
    fake.positions = [{"symbol": "BTCUSDT", "side": "Sell", "size": "5", "avgPrice": "100"}]
    fake.price = 98.0
    fake.calls.clear()
    r = await eng.execute_increase(pos, 2.0)
    assert r["success"] and r["size"] == 7.0
    (order,) = [c for n, c in fake.calls if n == "place_market_order"]
    assert (order["side"], order["qty"]) == ("Sell", "2.0") and order["order_link_id"].startswith(f"rl-d{dep}-")
    assert "set_trading_stop" not in fake.names()                    # the Full-mode stop covers the add
    assert (await _adds(db, pos["id"]))[-1]["side"] == "SELL"


async def test_pnl_after_adds_and_partial_closes_is_exact(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)                                             # 5 @ 100
    fake.price = 110.0
    (full,) = [p for p in await eng._own_open_positions() if p["id"] == pos["id"]]
    assert (await eng.execute_partial_exit(full, 40, reason="test"))["success"]  # 2 @ 110: +20
    fake.price = 120.0
    assert (await eng.execute_increase(pos, 4.0))["success"]          # 3 @ 100 + 4 @ 120
    row = await _row(db, pos["id"])
    assert row["size"] == 7.0 and row["entry_price"] == pytest.approx((3 * 100 + 4 * 120) / 7)
    fake.price = 130.0
    await eng.execute_exit(pos, reason="test")
    row = await _row(db, pos["id"])
    # lot by lot: 2 x (110-100) + 3 x (130-100) + 4 x (130-120) = 20 + 90 + 40
    assert (row["status"], row["pnl"], row["pnl_source"]) == ("CLOSED", pytest.approx(150.0), "dry_run")


async def test_the_entry_checks_apply_to_an_add(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    eng.halted_reason = "emergency_stop"
    assert (await eng.execute_increase(pos, 1.0))["reason"] == "engine_halted"
    eng.halted_reason = None
    assert (await eng.execute_increase(pos, 0))["reason"] == "invalid"
    assert (await eng.execute_increase(pos, 0.001))["reason"] == "qty_below_minimum"
    eng._own_close(pos["id"])
    assert (await eng.execute_increase(pos, 1.0))["reason"] == "not_open"


async def test_a_disabled_deployment_takes_no_adds(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    eng._settings_provider = lambda: {**SETTINGS, "enabled": False}
    assert (await eng.execute_increase(pos, 1.0))["reason"] == "deployment_disabled"


async def test_the_exposure_limit_counts_the_add(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "max_exposure_pct": 7.0})   # 700 of 10000
    pos = await _open(eng)                                             # 500 open
    r = await eng.execute_increase(pos, 3.0)                           # +300 = 800
    assert r["reason"] == "risk_check_failed" and "Exposure limit" in r["error"]
    assert (await eng.execute_increase(pos, 2.0))["success"]          # +200 = 700


def _rails(**kw):
    base = {"symbols": {"allow": [], "deny": []}, "max_notional_usd": None, "max_notional_pct": None,
            "max_gross_leverage": None, "on_limit": "clip", "trading_hours": None, "stop_before_bankruptcy": True}
    return {**SETTINGS, "rails": {**base, **kw}}


async def test_max_notional_counts_the_whole_position_after_the_add(db):
    eng, _ = await _engine(db, FakeBybit(), settings=_rails(max_notional_usd=800))
    pos = await _open(eng)                                             # 500
    r = await eng.execute_increase(pos, 5.0)                           # would be 1000
    assert r["success"] and r["added_quantity"] == 3.0 and r["sizing"]["rails"] == "clipped to max_notional_usd"
    r = await eng.execute_increase(pos, 1.0)                           # full now
    assert r["reason"] == "rail_max_notional_usd"


async def test_reject_mode_and_the_gross_leverage_rail(db):
    eng, _ = await _engine(db, FakeBybit(), settings=_rails(max_gross_leverage=0.06, on_limit="reject"))   # 600
    pos = await _open(eng)                                             # 500
    assert (await eng.execute_increase(pos, 2.0))["reason"] == "rail_max_gross_leverage"   # 700
    assert (await eng.execute_increase(pos, 1.0))["success"]          # 600


async def test_symbol_rails_apply_to_an_add(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    eng._settings_provider = lambda: _rails(symbols={"allow": [], "deny": ["BTC/USDT:USDT"]})
    assert (await eng.execute_increase(pos, 1.0))["reason"] == "symbol_not_allowed"


async def test_an_unconfirmed_add_leaves_the_pnl_to_the_venue(db):
    class _Unconfirmed(FakeBybit):
        async def get_order_history(self, **kw):
            return []
    fake = _Unconfirmed()
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    pos = await _open(eng)
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100"}]   # not grown yet
    eng.bybit = fake
    eng.order_executor.bybit = fake
    r = await eng.execute_increase(pos, 1.0)
    assert r["success"] and r["fill_price"] == 100.0
    row = await _row(db, pos["id"])
    meta = row["entry_meta"] if isinstance(row["entry_meta"], dict) else __import__("json").loads(row["entry_meta"])
    assert meta["entry_price_source"] == "expected_unconfirmed"


async def test_an_add_whose_record_fails_is_reported_loudly(db, monkeypatch):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    real = eng.order_executor.place_order

    async def closes_meanwhile(**kw):
        async with db.acquire() as c:
            await c.execute("UPDATE positions SET status = 'CLOSED' WHERE id = $1", pos["id"])
        return await real(**kw)
    monkeypatch.setattr(eng.order_executor, "place_order", closes_meanwhile)
    r = await eng.execute_increase(pos, 1.0)
    assert r["reason"] == "not_recorded" and "no longer open" in r["error"]
    assert len(await _adds(db, pos["id"])) == 1                      # only the opening entry


async def test_an_add_above_zero_but_below_the_venue_minimum_is_refused(db):
    fake = FakeBybit(lot={"qtyStep": "0.01", "minOrderQty": "0.5", "maxOrderQty": "1000"})
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    fake.calls.clear()
    r = await eng.execute_increase(pos, 0.2)                           # 0.2 > 0, < 0.5
    assert r["reason"] == "qty_below_minimum" and "place_market_order" not in fake.names()



# ---- review round (R3) -------------------------------------------------------------------------

async def test_an_unconfirmed_add_is_read_back_from_the_venue_position(db):
    class _Unconfirmed(FakeBybit):
        async def get_order_history(self, **kw):
            return []

        async def place_market_order(self, **kw):
            r = await super().place_market_order(**kw)
            self.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "6.5", "avgPrice": "102"}]   # 1.5 filled
            return r
    fake = _Unconfirmed()
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    pos = await _open(eng)                                             # 5 @ 100
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100"}]
    eng.bybit = fake
    eng.order_executor.bybit = fake
    r = await eng.execute_increase(pos, 2.0)                           # asked 2, the venue filled 1.5
    assert r["success"] and r["added_quantity"] == 1.5 and r["size"] == 6.5 and r["entry_price"] == 102.0
    row = await _row(db, pos["id"])
    meta = row["entry_meta"] if isinstance(row["entry_meta"], dict) else __import__("json").loads(row["entry_meta"])
    assert meta.get("entry_price_source") != "expected_unconfirmed"
    assert (await _adds(db, pos["id"]))[-1]["price"] == pytest.approx((102 * 6.5 - 100 * 5) / 1.5)


async def test_a_live_add_onto_a_venue_position_that_differs_is_refused(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.positions = []                                                # the stop fired on the venue
    fake.calls.clear()
    r = await eng.execute_increase(pos, 1.0)
    assert r["reason"] == "out_of_sync" and "place_market_order" not in fake.names()


@pytest.mark.parametrize("rule", ["trailing", "break_even", "ladder"])
async def test_a_position_with_rules_for_its_size_takes_no_add(db, rule):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    if rule == "trailing":
        assert (await eng.set_trailing(pos, 1.0))["success"]
    elif rule == "break_even":
        assert (await eng.set_break_even(pos, trigger_pct=2.0))["success"]
    else:
        assert (await eng.set_take_profits(pos, [{"pct": 5, "close_pct": 50}]))["success"]
    r = await eng.execute_increase(pos, 1.0)
    assert r["reason"] == "managed_position"


async def test_a_ladder_set_after_an_add_still_removes_the_entrys_take_profit(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng, take_profit_price=120.0)
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100"}]
    assert (await eng.execute_increase(pos, 1.0))["success"]
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "6", "avgPrice": "100"}]
    fake.calls.clear()
    assert (await eng.set_take_profits(pos, [{"pct": 5, "close_pct": 50}]))["success"]
    removed = [c for n, c in fake.calls if n == "set_trading_stop" and c.get("take_profit") == 0]
    assert removed, fake.calls


async def test_pnl_percent_uses_the_real_cost_when_adds_follow_partial_closes(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng, size={"qty": 10})                           # 10 @ 100 = 1000
    (full,) = [p for p in await eng._own_open_positions() if p["id"] == pos["id"]]
    assert (await eng.execute_partial_exit(full, 50, reason="test"))["success"]   # 5 @ 100: 0
    fake.price = 200.0
    assert (await eng.execute_increase(pos, 5.0))["success"]          # 5 @ 200 = 1000; average 150
    fake.price = 210.0
    await eng.execute_exit(pos, reason="test")                         # 10 @ 210
    row = await _rows(db, "SELECT pnl, pnl_percent FROM positions WHERE id = $1", pos["id"])
    # PnL: 5 x 0 + 5 x 110 + 5 x 10 = 600; cost 1000 + 1000 = 2000 -> 30 % (not 600 / (150 x 15))
    assert row[0]["pnl"] == pytest.approx(600.0) and row[0]["pnl_percent"] == pytest.approx(30.0)
