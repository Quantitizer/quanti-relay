"""Position management: moving a stop (engine + API), the current stop the audit re-arms and the sync
reads from the venue; the trailing stop."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from relay.execution.position_tracker import PositionTracker
from relay.execution.take_profit_ladder import ladder_price_problem, ladder_problem, plan_ladder
from tests.test_api import api  # noqa: F401  (fixture)
from tests.test_engine import SETTINGS, FakeBybit, _engine, _no_sleep, _rows, _sig  # noqa: F401
from tests.test_position_tracker import _FakeBybit, _pos, _setup


async def _open(eng, **kw):
    r = await eng.execute_entry(_sig(**kw))
    assert r["success"], r
    return {"id": r["position_id"]}


async def _stop(db, pid):
    (row,) = await _rows(db, "SELECT stop_price, initial_stop_price FROM positions WHERE id = $1", pid)
    return row["stop_price"], row["initial_stop_price"]


async def test_entry_records_the_opening_stop_as_the_current_one(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    assert await _stop(db, pos["id"]) == (95.0, 95.0)


async def test_paper_stop_move_is_recorded_and_sends_nothing(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    r = await eng.move_stop(pos, 98.0)
    assert r["success"] and r["stop_price"] == 98.0 and r["previous_stop"] == 95.0
    assert r["direction"] == "tightened"
    assert await _stop(db, pos["id"]) == (98.0, 95.0)          # the opening stop is kept
    assert "set_trading_stop" not in fake.names()


async def test_live_stop_move_is_tick_aligned_toward_entry_and_sent(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.calls.clear()
    r = await eng.move_stop(pos, 97.04)
    assert r["success"] and r["stop_price"] == pytest.approx(97.1)
    assert any("tick" in w for w in r["warnings"])
    (call,) = [c for n, c in fake.calls if n == "set_trading_stop"]
    assert call["symbol"] == "BTCUSDT" and call["stop_loss"] == pytest.approx(97.1)
    assert (await _stop(db, pos["id"]))[0] == pytest.approx(97.1)


@pytest.mark.parametrize(("side", "new", "direction"), [
    ("long", 92.0, "loosened"), ("short", 103.0, "tightened"), ("short", 108.0, "loosened")])
async def test_direction_is_reported_per_side(db, side, new, direction):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng, direction=side, stop_price=95.0 if side == "long" else 105.0)
    r = await eng.move_stop(pos, new)
    assert r["success"] and r["direction"] == direction


async def test_a_stop_in_profit_is_allowed(db):
    """Locking in a gain: a long's stop above entry, while the price is above it."""
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    fake.price = 110.0
    r = await eng.move_stop(pos, 104.0)
    assert r["success"] and r["stop_price"] == 104.0


@pytest.mark.parametrize(("side", "stop"), [("long", 100.0), ("long", 101.0), ("short", 100.0), ("short", 99.0)])
async def test_a_stop_the_price_has_already_passed_is_refused_and_the_old_one_stays(db, side, stop):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng, direction=side, stop_price=95.0 if side == "long" else 105.0)
    fake.calls.clear()
    r = await eng.move_stop(pos, stop)
    assert not r["success"] and r["code"] == "stop_already_breached"
    assert "set_trading_stop" not in fake.names()
    assert (await _stop(db, pos["id"]))[0] == (95.0 if side == "long" else 105.0)


async def test_max_stop_distance_applies_only_on_the_loss_side(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings={**SETTINGS, "max_stop_distance_pct": 6})
    pos = await _open(eng)
    r = await eng.move_stop(pos, 93.0)                          # 7% below entry
    assert not r["success"] and r["code"] == "stop_too_far"
    assert (await _stop(db, pos["id"]))[0] == 95.0
    fake.price = 120.0
    r = await eng.move_stop(pos, 110.0)                         # 10% ABOVE entry: a locked-in gain
    assert r["success"]


async def test_no_max_distance_unless_configured(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    assert (await eng.move_stop(pos, 50.0))["success"]


async def test_a_venue_position_shared_with_another_row_is_refused(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    async with db.acquire() as c:                               # an imported row, same venue position
        await c.execute("INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, created_at,"
                        " updated_at) SELECT symbol, side, 1, 100, 'OPEN', opened_at, opened_at, opened_at"
                        " FROM positions WHERE id = $1", pos["id"])
    fake.calls.clear()
    r = await eng.move_stop(pos, 97.0)
    assert not r["success"] and r["code"] == "shared_venue_position"
    assert "set_trading_stop" not in fake.names()


async def test_a_position_being_closed_is_not_touched(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    assert eng._own_close(pos["id"])
    r = await eng.move_stop(pos, 97.0)
    assert not r["success"] and r["code"] == "not_open"
    eng._release_close(pos["id"])
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", pos["id"])
    assert (await eng.move_stop(pos, 97.0))["code"] == "not_open"


async def test_a_stop_the_venue_refuses_leaves_the_record_unchanged(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.stop_ok = False
    r = await eng.move_stop(pos, 97.0)
    assert not r["success"] and r["code"] == "venue_rejected"
    assert (await _stop(db, pos["id"]))[0] == 95.0


async def test_no_price_means_no_move(db):
    class _NoPrice(FakeBybit):
        async def get_ticker(self, symbol, category="linear"):
            raise RuntimeError("venue down")
    fake = _NoPrice()
    eng, _ = await _engine(db, fake, settings={**SETTINGS})
    async with db.acquire() as c:
        now = datetime.now(UTC)
        pid = await c.fetchval(
            "INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, dry_run, opened_at,"
            " stop_price, created_at, updated_at) VALUES ($1, 'BTCUSDT', 'LONG', 1, 100, 'OPEN', TRUE, $2, 95,"
            " $2, $2) RETURNING id", eng.deployment_id, now)
    r = await eng.move_stop({"id": pid}, 97.0)
    assert not r["success"] and r["code"] == "no_price"
    eng.bybit = FakeBybit(price=0.0)                            # a zero price is no price either
    assert (await eng.move_stop({"id": pid}, 97.0))["code"] == "no_price"


async def test_the_stop_audit_rearms_the_current_stop_not_the_opening_one(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    await eng.move_stop(pos, 98.0)
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100", "markPrice": "101",
                       "stopLoss": ""}]
    fake.calls.clear()
    out = await eng.audit_stops()
    assert out["rearmed"] == 1
    assert dict(fake.calls)["set_trading_stop"]["stop_loss"] == pytest.approx(98.0)


async def test_the_sync_reads_the_current_stop_from_the_venue_and_keeps_it_when_the_venue_has_none(db):
    dep, _ = await _setup(db)
    from tests.test_position_tracker import _open as _tracker_open
    pid = await _tracker_open(db, "BTCUSDT", "LONG", dep)
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET stop_price = 95 WHERE id = $1", pid)
    await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1, stop="97.5")])).sync_all_positions()
    assert (await _stop(db, pid))[0] == 97.5                     # e.g. a trailing stop moved it
    await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1, stop="")])).sync_all_positions()
    assert (await _stop(db, pid))[0] == 97.5                     # kept for the stop audit to re-arm


# ---- API --------------------------------------------------------------------------------

async def test_api_moves_the_stop_and_explains_refusals(api):  # noqa: F811
    client, fake, _ = api
    r = (await client.post("/v1/orders", json={"symbol": "BTCUSDT", "side": "long",
                                               "size": {"notional_usd": 500}, "stop_price": 95})).json()
    pid = r["result"]["position_id"]
    ok = await client.post(f"/v1/positions/{pid}/stop", json={"stop_price": 97})
    assert ok.status_code == 200 and ok.json()["stop_price"] == 97 and ok.json()["direction"] == "tightened"
    (pos,) = (await client.get("/v1/positions")).json()
    assert pos["stop_price"] == 97 and pos["initial_stop_price"] == 95

    breached = await client.post(f"/v1/positions/{pid}/stop", json={"stop_price": 101})
    assert breached.status_code == 422 and "already passed" in breached.json()["detail"]
    assert (await client.post(f"/v1/positions/{pid}/stop", json={"stop_price": -1})).status_code == 422
    assert (await client.post(f"/v1/positions/{pid}/stop", json={"stop": 97})).status_code == 422
    assert (await client.post("/v1/positions/999/stop", json={"stop_price": 97})).status_code == 404


async def test_api_stop_move_on_a_closing_position_is_a_409(api):  # noqa: F811
    client, _, rt = api
    r = (await client.post("/v1/orders", json={"symbol": "BTCUSDT", "side": "long",
                                               "size": {"notional_usd": 500}, "stop_price": 95})).json()
    pid = r["result"]["position_id"]
    rt.engine._own_close(pid)
    assert (await client.post(f"/v1/positions/{pid}/stop", json={"stop_price": 97})).status_code == 409


# ---- trailing stop ------------------------------------------------------------------------

def _trailing_calls(fake):
    return [c for n, c in fake.calls if n == "set_trading_stop" and "trailing_stop" in c
            and c["trailing_stop"] is not None]


async def _management(db, pid):
    (row,) = await _rows(db, "SELECT management FROM positions WHERE id = $1", pid)
    m = row["management"]
    return json.loads(m) if isinstance(m, str) else m


async def test_paper_trailing_is_recorded_and_sends_nothing(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    r = await eng.set_trailing(pos, 1.0, 2.0)
    assert r["success"]
    t = r["trailing"]
    assert (t["distance_pct"], t["activate_pct"], t["active_price"]) == (1.0, 2.0, 102.0)
    assert t["distance"] == pytest.approx(1.0)                 # 1% of 102 = 1.02 -> the 0.1 tick
    assert (await _management(db, pos["id"]))["trailing"]["distance"] == pytest.approx(1.0)
    assert "set_trading_stop" not in fake.names()


async def test_live_trailing_is_sent_beside_the_fixed_stop_which_is_not_touched(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.calls.clear()
    r = await eng.set_trailing(pos, 2.0, 5.0)
    assert r["success"]
    (call,) = [c for n, c in fake.calls if n == "set_trading_stop"]
    assert call["trailing_stop"] == pytest.approx(2.1) and call["active_price"] == pytest.approx(105.0)
    assert call.get("stop_loss") is None and call["tpsl_mode"] == "Full"
    assert await _stop(db, pos["id"]) == (95.0, 95.0)


async def test_short_trailing_activates_below_entry(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng, direction="short", stop_price=105.0)
    r = await eng.set_trailing(pos, 1.0, 3.0)
    assert r["trailing"]["active_price"] == pytest.approx(97.0)
    assert _trailing_calls(fake)[-1]["active_price"] == pytest.approx(97.0)


async def test_trailing_without_activation_trails_from_the_current_price(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.price = 110.0
    r = await eng.set_trailing(pos, 1.0)
    assert r["trailing"]["active_price"] is None and r["trailing"]["distance"] == pytest.approx(1.1)
    assert _trailing_calls(fake)[-1]["active_price"] is None


async def test_an_activation_already_reached_trails_at_once_and_says_so(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.price = 104.0
    r = await eng.set_trailing(pos, 1.0, 3.0)                    # 103 already passed
    assert r["success"] and r["trailing"]["active_price"] is None
    assert any("trails from now" in w for w in r["warnings"])


async def test_a_tiny_trailing_distance_is_at_least_one_tick(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    assert (await eng.set_trailing(pos, 0.01))["trailing"]["distance"] == pytest.approx(0.1)


async def test_removing_the_trailing_stop_cancels_it_and_keeps_the_rest(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    await eng.set_trailing(pos, 1.0)
    fake.calls.clear()
    r = await eng.set_trailing(pos, None)
    assert r["success"] and r["trailing"] is None
    (call,) = [c for n, c in fake.calls if n == "set_trading_stop"]
    assert call["trailing_stop"] == 0 and call.get("stop_loss") is None
    assert await _management(db, pos["id"]) is None
    assert (await _stop(db, pos["id"]))[0] == 95.0


@pytest.mark.parametrize(("distance", "activate"), [
    (0, 2.0), (-1, None), (100, None), (150, None), (1.0, -0.5), (float("nan"), None), (1.0, float("inf"))])
async def test_unusable_trailing_inputs_are_refused_before_anything_is_sent(db, distance, activate):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.calls.clear()
    r = await eng.set_trailing(pos, distance, activate)
    assert not r["success"] and r["code"] == "invalid"
    assert fake.calls == []


async def test_a_trailing_stop_the_venue_refuses_is_not_recorded(db):
    class _NoTrailing(FakeBybit):
        async def set_trading_stop(self, **kw):
            if kw.get("trailing_stop") is not None:
                self._rec("set_trading_stop", **kw)
                return {"retCode": 10001, "retMsg": "TrailingStop invalid"}
            return await super().set_trading_stop(**kw)
    fake = _NoTrailing()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    r = await eng.set_trailing(pos, 1.0)
    assert not r["success"] and r["code"] == "venue_rejected"
    assert await _management(db, pos["id"]) is None


async def test_trailing_on_a_shared_venue_position_or_a_closing_one_is_refused(db):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    pos = await _open(eng)
    eng._own_close(pos["id"])
    assert (await eng.set_trailing(pos, 1.0))["code"] == "not_open"
    eng._release_close(pos["id"])
    async with db.acquire() as c:
        await c.execute("INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, created_at,"
                        " updated_at) SELECT symbol, side, 1, 100, 'OPEN', opened_at, opened_at, opened_at"
                        " FROM positions WHERE id = $1", pos["id"])
    assert (await eng.set_trailing(pos, 1.0))["code"] == "shared_venue_position"


# ---- trailing at entry ---------------------------------------------------------------------

async def test_an_orders_trailing_stop_goes_on_after_the_fixed_stop(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(trailing={"distance_pct": 1.0, "activate_pct": 2.0}))
    assert r["success"] and r["trailing"]["active_price"] == pytest.approx(102.0) and r["trailing_note"] is None
    stops = [c for n, c in fake.calls if n == "set_trading_stop"]
    assert stops[0]["stop_loss"] == pytest.approx(95.0) and stops[-1]["trailing_stop"] == pytest.approx(1.0)


async def test_no_trailing_stop_unless_configured(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    assert r["success"] and r["trailing"] is None and not _trailing_calls(fake)


async def test_the_deployments_trailing_applies_when_the_order_sends_none(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "trailing_distance_pct": 1.5,
                                                      "trailing_activate_pct": 2.0})
    r = await eng.execute_entry(_sig())
    assert (r["trailing"]["distance_pct"], r["trailing"]["activate_pct"]) == (1.5, 2.0)


async def test_an_orders_trailing_replaces_the_deployments_as_a_whole(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "trailing_distance_pct": 1.5,
                                                      "trailing_activate_pct": 2.0})
    r = await eng.execute_entry(_sig(trailing={"distance_pct": 1.0}))
    assert (r["trailing"]["distance_pct"], r["trailing"]["activate_pct"]) == (1.0, None)


@pytest.mark.parametrize("settings", [{"trailing_activate_pct": 2.0}, {"trailing_distance_pct": 120}])
async def test_unusable_trailing_settings_refuse_entries_before_any_order(db, settings):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, **settings})
    r = await eng.execute_entry(_sig())
    assert not r["success"] and r["reason"].startswith("trailing_invalid")
    assert "place_market_order" not in fake.names()


async def test_a_refused_trailing_stop_at_entry_keeps_the_protected_position(db):
    class _NoTrailing(FakeBybit):
        async def set_trading_stop(self, **kw):
            if kw.get("trailing_stop") is not None:
                return {"retCode": 10001, "retMsg": "TrailingStop invalid"}
            return await super().set_trading_stop(**kw)
    eng, _ = await _engine(db, _NoTrailing(), dry_run=False)
    r = await eng.execute_entry(_sig(trailing={"distance_pct": 1.0}))
    assert r["success"] and r["trailing"] is None and r["trailing_note"].startswith("trailing_not_set")
    (row,) = await _rows(db, "SELECT status, stop_price FROM positions WHERE id = $1", r["position_id"])
    assert (row["status"], row["stop_price"]) == ("OPEN", 95.0)


# ---- the sync and a trailing stop removed on the venue -------------------------------------

async def _tracked_with_trailing(db, set_at):
    dep, _ = await _setup(db)
    from tests.test_position_tracker import _open as _tracker_open
    pid = await _tracker_open(db, "BTCUSDT", "LONG", dep)
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET management = $1 WHERE id = $2",
                        json.dumps({"trailing": {"distance": 1.0, "set_at": set_at.isoformat()}, "other": 1}), pid)
    return pid


async def test_the_sync_drops_a_trailing_stop_removed_on_the_venue(db):
    pid = await _tracked_with_trailing(db, datetime.now(UTC) - timedelta(minutes=5))
    await PositionTracker(db, _FakeBybit([{**_pos("BTCUSDT", "Buy", 1), "trailingStop": "0"}])).sync_all_positions()
    assert await _management(db, pid) == {"other": 1}


async def test_the_sync_keeps_a_trailing_stop_the_venue_shows_or_one_set_during_the_sync(db):
    pid = await _tracked_with_trailing(db, datetime.now(UTC) - timedelta(minutes=5))
    await PositionTracker(db, _FakeBybit([{**_pos("BTCUSDT", "Buy", 1), "trailingStop": "780"}])).sync_all_positions()
    assert "trailing" in await _management(db, pid)
    await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1)])).sync_all_positions()   # field absent
    assert "trailing" in await _management(db, pid)
    async with db.acquire() as c:                               # set after the venue was read
        await c.execute("UPDATE positions SET management = $1 WHERE id = $2", json.dumps(
            {"trailing": {"set_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}}), pid)
    await PositionTracker(db, _FakeBybit([{**_pos("BTCUSDT", "Buy", 1), "trailingStop": "0"}])).sync_all_positions()
    assert "trailing" in await _management(db, pid)


# ---- trailing through the API ---------------------------------------------------------------

async def test_api_trailing_on_an_order_and_on_an_open_position(api):  # noqa: F811
    client, fake, _ = api
    r = (await client.post("/v1/orders", json={"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500},
                                               "stop_price": 95, "trailing": {"distance_pct": 1}})).json()
    assert r["status"] == "executed" and r["result"]["trailing"]["distance_pct"] == 1
    pid = r["result"]["position_id"]
    ok = await client.post(f"/v1/positions/{pid}/trailing", json={"distance_pct": 2, "activate_pct": 3})
    assert ok.status_code == 200 and ok.json()["trailing"]["active_price"] == 103
    (pos,) = (await client.get("/v1/positions")).json()
    assert pos["management"]["trailing"]["distance_pct"] == 2 and pos["stop_price"] == 95
    gone = await client.delete(f"/v1/positions/{pid}/trailing")
    assert gone.status_code == 200 and gone.json()["trailing"] is None
    (pos,) = (await client.get("/v1/positions")).json()
    assert pos["management"] is None and pos["stop_price"] == 95
    for bad in ({"distance_pct": 0}, {"distance_pct": 100}, {"distance_pct": 1, "activate_pct": -1}, {}):
        assert (await client.post(f"/v1/positions/{pid}/trailing", json=bad)).status_code == 422
    bad_order = {"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95,
                 "trailing": {"distance_pct": 1, "pct": 2}}
    assert (await client.post("/v1/orders", json=bad_order)).status_code == 422


# ---- break-even -----------------------------------------------------------------------------

async def _be(db, pid):
    return ((await _management(db, pid)) or {}).get("break_even")


@pytest.mark.parametrize(("args", "problem"), [
    ((None, None, None), None), ((1.0, None, None), None), ((None, 1.5, 0.1), None),
    ((1.0, 1.0, None), "set one"), ((0, None, None), "more than 0"), ((None, -1, None), "more than 0"),
    ((None, None, 0.1), "needs a break-even trigger"), ((1.0, None, -0.1), "0 % or more"),
    ((1.0, None, 1.0), "smaller than its trigger"), ((float("nan"), None, None), "number"),
    ((1.0, None, float("inf")), "number")])
def test_break_even_inputs(args, problem):
    from relay.execution.engine import break_even_problem
    got = break_even_problem(*args)
    assert (got is None) if problem is None else (problem in got)


@pytest.mark.parametrize(("side", "entry", "stop", "plan", "expected"), [
    ("long", 100, 95, {"trigger_pct": 2}, (102.0, 100.0)),
    ("long", 100, 95, {"trigger_r": 1}, (105.0, 100.0)),
    ("long", 100, 95, {"trigger_r": 1, "buffer_pct": 0.2}, (105.0, 100.2)),
    ("short", 100, 104, {"trigger_r": 2}, (92.0, 100.0)),
    ("short", 100, 104, {"trigger_pct": 3, "buffer_pct": 0.5}, (97.0, 99.5))])
def test_break_even_levels(side, entry, stop, plan, expected):
    from relay.execution.engine import break_even_levels
    assert break_even_levels(side, entry, stop, plan, 0.1) == pytest.approx(expected)


@pytest.mark.parametrize(("side", "stop", "plan"), [
    ("long", None, {"trigger_r": 1}), ("long", 101, {"trigger_r": 1}),       # no / wrong-side stop
    ("short", 99, {"trigger_r": 1}),
    ("long", 99.9, {"trigger_r": 1, "buffer_pct": 0.5})])                     # buffer past the trigger
def test_break_even_levels_that_cannot_work(side, stop, plan):
    from relay.execution.engine import break_even_levels
    with pytest.raises(ValueError):
        break_even_levels(side, 100, stop, plan, 0.1)


async def test_break_even_moves_the_stop_once_the_trigger_is_reached(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    r = await eng.set_break_even(pos, trigger_r=1.0, buffer_pct=0.2)
    assert r["success"] and r["break_even"]["trigger_price"] == pytest.approx(105.0)
    assert r["break_even"]["stop_target"] == pytest.approx(100.2)
    fake.price = 104.0
    out = await eng.run_stop_rules()
    assert (out["checked"], out["waiting"], out["moved"]) == (1, 1, [])
    assert (await _stop(db, pos["id"]))[0] == 95.0
    fake.calls.clear()
    fake.price = 105.0
    out = await eng.run_stop_rules()
    assert out["moved"] == [pos["id"]]
    (call,) = [c for n, c in fake.calls if n == "set_trading_stop"]
    assert call["stop_loss"] == pytest.approx(100.2)
    assert (await _stop(db, pos["id"]))[0] == pytest.approx(100.2)
    be = await _be(db, pos["id"])
    assert be["done_at"] and be["moved"] is True and be["stop"] == pytest.approx(100.2)
    fake.calls.clear()
    assert (await eng.run_stop_rules())["checked"] == 0              # done: never moved again
    assert fake.calls == []


async def test_break_even_works_for_paper_positions(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng, direction="short", stop_price=105.0)
    await eng.set_break_even(pos, trigger_pct=2.0)
    fake.price = 97.9
    assert (await eng.run_stop_rules())["moved"] == [pos["id"]]
    assert (await _stop(db, pos["id"]))[0] == pytest.approx(100.0)
    assert "set_trading_stop" not in fake.names()


async def test_break_even_never_loosens_a_stop_already_past_its_target(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    await eng.set_break_even(pos, trigger_pct=2.0)
    fake.price = 110.0
    await eng.move_stop(pos, 106.0)                                  # the user locked in more already
    fake.calls.clear()
    out = await eng.run_stop_rules()
    assert out["already_tighter"] == [pos["id"]] and out["moved"] == []
    assert "set_trading_stop" not in fake.names()
    assert (await _stop(db, pos["id"]))[0] == 106.0
    assert (await _be(db, pos["id"]))["moved"] is False


async def test_a_refused_break_even_move_stays_armed_and_is_retried(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    await eng.set_break_even(pos, trigger_pct=2.0)
    fake.price = 102.0
    fake.stop_ok = False
    out = await eng.run_stop_rules()
    assert [x["code"] for x in out["refused"]] == ["venue_rejected"] and out["moved"] == []
    assert not (await _be(db, pos["id"]))["done_at"]
    fake.stop_ok = True
    assert (await eng.run_stop_rules())["moved"] == [pos["id"]]


async def test_break_even_waits_without_a_price_and_skips_positions_being_closed(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    await eng.set_break_even(pos, trigger_pct=2.0)
    fake.price = 0.0
    assert (await eng.run_stop_rules())["waiting"] == 1
    fake.price = 103.0
    eng._own_close(pos["id"])
    assert (await eng.run_stop_rules())["checked"] == 0
    eng._release_close(pos["id"])
    assert (await eng.run_stop_rules())["moved"] == [pos["id"]]


async def test_break_even_follows_the_confirmed_entry_price(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    await eng.set_break_even(pos, trigger_pct=2.0)
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET entry_price = 101 WHERE id = $1", pos["id"])
    fake.price = 102.5                                              # 2% above 100, not above 101
    assert (await eng.run_stop_rules())["moved"] == []
    fake.price = 103.1
    assert (await eng.run_stop_rules())["moved"] == [pos["id"]]
    assert (await _stop(db, pos["id"]))[0] == pytest.approx(101.0)


async def test_a_rearmed_break_even_is_not_marked_done_by_the_old_pass(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    await eng.set_break_even(pos, trigger_pct=2.0)
    old = await _be(db, pos["id"])
    await eng.set_break_even(pos, trigger_pct=5.0)
    await eng._finish_break_even(pos["id"], old, 100.0, moved=True)
    assert (await _be(db, pos["id"]))["done_at"] is None


async def test_removing_break_even_keeps_the_stop_and_the_trailing_stop(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    await eng.set_trailing(pos, 1.0)
    await eng.set_break_even(pos, trigger_pct=2.0)
    assert set(await _management(db, pos["id"])) == {"trailing", "break_even"}
    r = await eng.set_break_even(pos)
    assert r["success"] and r["break_even"] is None
    assert set(await _management(db, pos["id"])) == {"trailing"}
    assert (await _stop(db, pos["id"]))[0] == 95.0


async def test_arming_break_even_that_cannot_work_is_refused(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "require_stop": False})
    r = await eng.execute_entry(_sig(stop_price=None))
    pos = {"id": r["position_id"]}
    assert (await eng.set_break_even(pos, trigger_r=1.0))["code"] == "invalid"      # no opening stop
    assert (await eng.set_break_even(pos, trigger_pct=1.0, buffer_pct=2.0))["code"] == "invalid"
    assert await _be(db, pos["id"]) is None


async def test_an_already_reached_trigger_is_reported_when_arming(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    pos = await _open(eng)
    fake.price = 103.0
    r = await eng.set_break_even(pos, trigger_pct=2.0)
    assert any("already reached" in w for w in r["warnings"])
    assert (await _stop(db, pos["id"]))[0] == 95.0                   # moved by the next pass, not here


# ---- break-even at entry ---------------------------------------------------------------------

async def test_no_break_even_unless_configured(db):
    eng, _ = await _engine(db, FakeBybit())
    r = await eng.execute_entry(_sig())
    assert r["break_even"] is None and await _be(db, r["position_id"]) is None


async def test_an_orders_break_even_is_armed_at_entry(db):
    eng, _ = await _engine(db, FakeBybit())
    r = await eng.execute_entry(_sig(break_even={"trigger_r": 1.0}))
    assert r["break_even"]["trigger_price"] == pytest.approx(105.0) and r["break_even_note"] is None


async def test_the_deployments_break_even_applies_unless_the_order_has_its_own(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "break_even_trigger_pct": 3.0,
                                                      "break_even_buffer_pct": 0.5})
    r = await eng.execute_entry(_sig())
    assert (r["break_even"]["trigger_pct"], r["break_even"]["buffer_pct"]) == (3.0, 0.5)
    r = await eng.execute_entry(_sig(symbol="ETHUSDT", break_even={"trigger_r": 2.0}))
    assert (r["break_even"]["trigger_r"], r["break_even"]["trigger_pct"], r["break_even"]["buffer_pct"]) == \
        (2.0, None, None)


@pytest.mark.parametrize(("settings", "sig"), [
    ({"break_even_buffer_pct": 0.5}, {}),                                        # buffer without trigger
    ({"break_even_trigger_pct": 1.0, "break_even_trigger_r": 1.0}, {}),           # two triggers
    ({"require_stop": False, "break_even_trigger_r": 1.0}, {"stop_price": None}),  # R without a stop
])
async def test_unusable_break_even_refuses_entries_before_any_order(db, settings, sig):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, **settings})
    r = await eng.execute_entry(_sig(**sig))
    assert not r["success"] and r["reason"].startswith("break_even_invalid")
    assert "place_market_order" not in fake.names()


# ---- break-even through the API ----------------------------------------------------------------

async def test_api_break_even(api):  # noqa: F811
    client, fake, _ = api
    order = {"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95,
             "break_even": {"trigger_pct": 2}}
    r = (await client.post("/v1/orders", json=order)).json()
    assert r["status"] == "executed" and r["result"]["break_even"]["trigger_price"] == 102
    pid = r["result"]["position_id"]
    ok = await client.post(f"/v1/positions/{pid}/break-even", json={"trigger_r": 1, "buffer_pct": 0.1})
    assert ok.status_code == 200 and ok.json()["break_even"]["trigger_price"] == 105
    fake.price = 105.5
    ran = (await client.post("/v1/stop-rules")).json()
    assert ran["moved"] == [pid]
    (pos,) = (await client.get("/v1/positions")).json()
    assert pos["stop_price"] == 100.1 and pos["management"]["break_even"]["done_at"]
    gone = await client.delete(f"/v1/positions/{pid}/break-even")
    assert gone.status_code == 200
    (pos,) = (await client.get("/v1/positions")).json()
    assert pos["management"] is None and pos["stop_price"] == 100.1
    for bad in ({}, {"trigger_pct": 1, "trigger_r": 1}, {"trigger_pct": 1, "buffer_pct": 1},
                {"trigger_pct": -1}, {"trigger_pct": 1, "x": 1}):
        assert (await client.post(f"/v1/positions/{pid}/break-even", json=bad)).status_code == 422
    assert (await client.post("/v1/orders", json={**order, "symbol": "ETHUSDT",
                                                   "break_even": {"buffer_pct": 1}})).status_code == 422


# ---- take-profit ladder: the rules ----------------------------------------------------------


@pytest.mark.parametrize(("levels", "problem"), [
    ([{"pct": 2, "close_pct": 50}, {"price": 110, "close_pct": 50}], None),
    ([], "at least one"), (None, "at least one"), ([1], "object"),
    ([{"pct": 2}], "close_pct"), ([{"pct": 2, "close_pct": 0}], "close_pct"),
    ([{"pct": 2, "close_pct": 101}], "close_pct"), ([{"close_pct": 50}], "exactly one"),
    ([{"pct": 2, "price": 101, "close_pct": 50}], "exactly one"), ([{"pct": 0, "close_pct": 50}], "more than 0"),
    ([{"pct": 2, "close_pct": 60}, {"pct": 3, "close_pct": 50}], "at most 100"),
    ([{"pct": 3, "close_pct": 50}, {"pct": 2, "close_pct": 50}], "further from entry"),
    ([{"pct": float("nan"), "close_pct": 50}], "number"), ([{"pct": True, "close_pct": 50}], "number"),
    ([{"pct": 2, "close_pct": 50, "qty": 1}], "unknown")])
def test_ladder_rules(levels, problem):
    got = ladder_problem(levels)
    assert (got is None) if problem is None else (problem in got)


@pytest.mark.parametrize(("side", "levels", "problem"), [
    ("long", [{"price": 105, "close_pct": 50}, {"pct": 8, "close_pct": 50}], None),
    ("long", [{"price": 99, "close_pct": 50}], "profit side"),
    ("short", [{"price": 101, "close_pct": 50}], "profit side"),
    ("short", [{"price": 95, "close_pct": 50}, {"price": 97, "close_pct": 50}], "further from entry"),
    ("long", [{"price": 105, "close_pct": 50}, {"pct": 4, "close_pct": 50}], "further from entry")])
def test_ladder_prices(side, levels, problem):
    got = ladder_price_problem(side, 100, levels)
    assert (got is None) if problem is None else (problem in got)


@pytest.mark.parametrize(("size", "pcts", "step", "min_qty", "qtys", "statuses"), [
    (5.0, [50, 50], 0.01, 0.01, [2.5, 2.5], ["planned"] * 2),
    (1.0, [33, 33, 34], 0.01, 0.01, [0.33, 0.33, 0.34], ["planned"] * 3),
    (1.0, [30, 30], 0.01, 0.01, [0.3, 0.3], ["planned"] * 2),          # the rest rides with the stop
    (0.03, [20, 80], 0.01, 0.01, [0.0, 0.03], ["skipped", "planned"]),  # a share below the minimum moves on
    (0.07, [50, 50], 0.01, 0.01, [0.03, 0.04], ["planned"] * 2),        # never more than asked, rest on the last
    (0.2, [10, 90], 0.01, 0.05, [0.0, 0.2], ["skipped", "planned"])])   # above the step, below the minimum
def test_ladder_quantities(size, pcts, step, min_qty, qtys, statuses):
    plan = plan_ladder("long", 100, size, [{"pct": i + 1, "close_pct": c} for i, c in enumerate(pcts)],
                       0.1, step, min_qty)
    assert [lv["qty"] for lv in plan] == pytest.approx(qtys)
    assert [lv["status"] for lv in plan] == statuses


def test_ladder_prices_follow_the_side_and_tick():
    plan = plan_ladder("short", 100, 1, [{"pct": 2.04, "close_pct": 50}, {"price": 95.04, "close_pct": 50}],
                       0.1, 0.01, 0.01)
    assert [lv["price"] for lv in plan] == pytest.approx([98.0, 95.0])


# ---- take-profit ladder: on positions ----------------------------------------------------------

class LadderFake(FakeBybit):
    """FakeBybit with a book of resting orders that tests fill, cancel or let rest."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.book = {}                                    # orderId -> order dict

    async def place_limit_order(self, **kw):
        self._rec("place_limit_order", **kw)
        oid = f"tp{len(self.book) + 1}"
        self.book[oid] = {"orderId": oid, "orderStatus": "New", "qty": kw["qty"], "price": kw["price"],
                          "cumExecQty": "0", "avgPrice": "0", "symbol": kw["symbol"]}
        return {"orderId": oid}

    async def get_open_orders(self, symbol=None, **kw):
        return [o for o in self.book.values() if o["orderStatus"] in ("New", "PartiallyFilled")
                and (symbol is None or o["symbol"] == symbol)]

    async def get_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        return next((o for o in self.book.values() if o["orderId"] == order_id
                     or (order_link_id and o.get("orderLinkId") == order_link_id)), None)

    async def get_order_history(self, **kw):
        if kw.get("order_id") in self.book or kw.get("order_link_id"):
            return []                                     # lags: never shows a fresh level
        return await super().get_order_history(**kw)      # the entry's market order

    async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        self._rec("cancel_order", symbol=symbol, order_id=order_id)
        if order_id in self.book:
            self.book[order_id]["orderStatus"] = "Cancelled"
        return {}

    async def amend_order(self, **kw):
        self._rec("amend_order", **kw)
        self.book[kw["order_id"]]["qty"] = kw["qty"]
        return {}

    def fill(self, oid, qty=None, status="Filled"):
        o = self.book[oid]
        o.update(orderStatus=status, cumExecQty=str(qty if qty is not None else o["qty"]), avgPrice=o["price"])

    def limits(self):
        return [c for n, c in self.calls if n == "place_limit_order"]


LADDER = [{"pct": 2, "close_pct": 50}, {"pct": 4, "close_pct": 50}]


async def _levels(db, pid):
    return ((await _management(db, pid)) or {}).get("take_profits", {}).get("levels")


async def test_a_live_ladder_rests_as_reduce_only_limit_orders_after_the_stop(db):
    fake = LadderFake()
    eng, dep = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    assert r["success"] and r["take_profits_note"] is None
    a, b = fake.limits()
    assert (a["side"], a["qty"], a["price"], a["reduce_only"], a["post_only"]) == ("Sell", "2.50", "102.0", True, False)
    assert (b["qty"], b["price"]) == ("2.50", "104.0")
    assert a["order_link_id"].startswith(f"rl-d{dep}-")
    names = fake.names()
    assert names.index("set_trading_stop") < names.index("place_limit_order")
    assert [lv["status"] for lv in await _levels(db, r["position_id"])] == ["open", "open"]


async def test_a_paper_ladder_is_recorded_and_sends_nothing(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    assert [lv["order_id"] for lv in r["take_profits"]] == ["dry_run_tp1_BTCUSDT", "dry_run_tp2_BTCUSDT"]
    assert fake.limits() == []


async def test_filled_levels_are_recorded_as_partial_closes(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    pid = r["position_id"]
    assert (await eng.run_stop_rules())["tp_filled"] == []          # nothing filled yet
    fake.fill("tp1")
    out = await eng.run_stop_rules()
    assert out["tp_filled"] == [(pid, 1)]
    (t,) = await _rows(db, "SELECT qty, price, order_id, exit_reason FROM trades WHERE position_id = $1 "
                           "AND trade_type = 'partial_exit'", pid)
    assert (t["qty"], t["price"], t["order_id"], t["exit_reason"]) == (2.5, 102.0, "tp1", "take_profit")
    (p,) = await _rows(db, "SELECT size, status FROM positions WHERE id = $1", pid)
    assert (p["size"], p["status"]) == (2.5, "OPEN")
    fake.fill("tp2")                                               # the rest: the venue closed it
    assert (await eng.run_stop_rules())["tp_filled"] == [(pid, 2)]
    assert len(await _rows(db, "SELECT id FROM trades WHERE position_id = $1 AND trade_type = 'partial_exit'",
                           pid)) == 1                              # the close is the sync's to record
    assert [lv["status"] for lv in await _levels(db, pid)] == ["filled", "filled"]


async def test_a_paper_ladder_fills_when_the_price_gets_there(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    pid = r["position_id"]
    fake.price = 102.5
    assert (await eng.run_stop_rules())["tp_filled"] == [(pid, 1)]
    (p,) = await _rows(db, "SELECT size FROM positions WHERE id = $1", pid)
    assert p["size"] == 2.5
    fake.price = 104.0
    await eng.run_stop_rules()
    (p,) = await _rows(db, "SELECT status, exit_price FROM positions WHERE id = $1", pid)
    assert p["status"] == "CLOSED"


async def test_a_level_cancelled_on_the_venue_or_partly_filled_is_recorded_as_such(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    pid = r["position_id"]
    fake.book["tp1"]["orderStatus"] = "Cancelled"
    fake.fill("tp2", qty=1.0, status="Cancelled")                  # part filled, then cancelled
    out = await eng.run_stop_rules()
    assert out["tp_cancelled"] == [pid] and out["tp_filled"] == [(pid, 2)]
    levels = await _levels(db, pid)
    assert [lv["status"] for lv in levels] == ["cancelled", "filled"] and levels[1]["filled_qty"] == 1.0
    (p,) = await _rows(db, "SELECT size FROM positions WHERE id = $1", pid)
    assert p["size"] == 4.0


async def test_an_unreadable_order_book_changes_nothing(db):
    class _Blind(LadderFake):
        async def get_open_orders(self, symbol=None, **kw):
            raise RuntimeError("venue down")
    fake = _Blind()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    fake.fill("tp1")
    assert (await eng.run_stop_rules())["tp_filled"] == []
    assert [lv["status"] for lv in await _levels(db, r["position_id"])] == ["open", "open"]


async def test_levels_larger_than_the_position_are_shrunk_furthest_first(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    pid = r["position_id"]
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_partial_exit(pos, 60)                        # 5 -> 2: the ladder holds 5
    await eng.run_stop_rules()
    levels = await _levels(db, pid)
    assert [lv["status"] for lv in levels] == ["open", "cancelled"] and levels[0]["qty"] == pytest.approx(2.0)
    assert fake.book["tp2"]["orderStatus"] == "Cancelled" and fake.book["tp1"]["qty"] == "2.00"


async def test_a_new_ladder_cancels_the_old_one_first_and_delete_removes_it(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    pos = {"id": r["position_id"]}
    res = await eng.set_take_profits(pos, [{"price": 103, "close_pct": 100}])
    assert res["success"] and [fake.book[o]["orderStatus"] for o in ("tp1", "tp2")] == ["Cancelled"] * 2
    assert fake.book["tp3"]["qty"] == "5.00"
    gone = await eng.set_take_profits(pos, None)
    assert gone["success"] and fake.book["tp3"]["orderStatus"] == "Cancelled"
    assert await _management(db, pos["id"]) is None


async def test_levels_already_passed_or_refused_are_reported_and_the_rest_placed(db):
    class _Picky(LadderFake):
        async def place_limit_order(self, **kw):
            if kw["price"] == "106.0":
                raise RuntimeError("Bybit API error 10001: params error")
            return await super().place_limit_order(**kw)
    fake = _Picky()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    fake.price = 102.5
    res = await eng.set_take_profits(pos, [{"pct": 2, "close_pct": 30}, {"pct": 4, "close_pct": 30},
                                           {"pct": 6, "close_pct": 40}])
    assert res["success"]
    assert [lv["status"] for lv in res["take_profits"]] == ["skipped", "open", "failed"]
    assert len(res["warnings"]) == 2 and len(fake.limits()) == 1


async def test_a_ladder_replaces_a_whole_position_take_profit(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng, take_profit_price=110.0)
    fake.calls.clear()
    await eng.set_take_profits(pos, LADDER)
    tps = [c for n, c in fake.calls if n == "set_trading_stop" and c.get("take_profit") is not None]
    assert [c["take_profit"] for c in tps] == [0]


async def test_leftover_levels_of_a_closed_position_are_cancelled(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    async with db.acquire() as c:                                  # e.g. the stop closed it on the venue
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1 WHERE id = $2",
                        datetime.now(UTC), r["position_id"])
    assert (await eng.run_stop_rules())["tp_cleaned"] == 1
    assert [fake.book[o]["orderStatus"] for o in ("tp1", "tp2")] == ["Cancelled"] * 2
    assert (await eng.run_stop_rules())["tp_cleaned"] == 0


async def test_a_new_entry_first_cancels_leftover_levels_on_its_symbol(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1 WHERE id = $2",
                        datetime.now(UTC), r["position_id"])
    fake.calls.clear()
    assert (await eng.execute_entry(_sig()))["success"]
    names = fake.names()
    assert names.index("cancel_order") < names.index("place_market_order")


@pytest.mark.parametrize(("sig", "settings"), [
    ({"take_profits": [{"price": 99, "close_pct": 50}]}, {}),                 # wrong side
    ({"take_profits": LADDER, "take_profit_price": 110}, {}),                 # both on the order
    ({}, {"take_profit_ladder": LADDER, "take_profit_pct": 3}),               # both on the deployment
    ({"take_profits": [{"pct": 2, "close_pct": 80}, {"pct": 3, "close_pct": 80}]}, {}),
    ({"take_profits": LADDER, "break_even": {"trigger_tp": 3}}, {}),          # no level 3
])
async def test_unusable_take_profits_refuse_entries_before_any_order(db, sig, settings):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, **settings})
    r = await eng.execute_entry(_sig(**sig))
    assert not r["success"] and r["reason"].split(":")[0] in ("take_profit_invalid", "break_even_invalid")
    assert "place_market_order" not in fake.names()


async def test_the_deployments_ladder_applies_unless_the_order_brings_a_take_profit(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "take_profit_ladder": LADDER})
    r = await eng.execute_entry(_sig())
    assert len(r["take_profits"]) == 2
    r = await eng.execute_entry(_sig(symbol="ETHUSDT", take_profit_price=110.0))
    assert r["take_profits"] is None and r["take_profit_price"] == 110.0


async def test_an_orders_ladder_replaces_the_deployments_single_take_profit(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "take_profit_pct": 3})
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    assert r["take_profit_price"] is None and len(r["take_profits"]) == 2
    assert not [c for n, c in fake.calls if n == "set_trading_stop" and c.get("take_profit")]


async def test_break_even_after_the_first_take_profit(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER, break_even={"trigger_tp": 1}))
    pid = r["position_id"]
    assert r["break_even"]["trigger_tp"] == 1 and r["break_even"]["trigger_price"] is None
    fake.price = 103.0                                             # past level 1, but it has not filled
    assert (await eng.run_stop_rules())["moved"] == []
    fake.fill("tp1")
    out = await eng.run_stop_rules()
    assert out["tp_filled"] == [(pid, 1)] and out["moved"] == [pid]
    assert (await _stop(db, pid))[0] == pytest.approx(100.0)


async def test_break_even_after_a_level_the_position_does_not_have_is_refused(db):
    eng, _ = await _engine(db, LadderFake())
    pos = await _open(eng)
    assert (await eng.set_break_even(pos, trigger_tp=1))["code"] == "invalid"


async def test_api_take_profit_ladder(api):  # noqa: F811
    client, fake, _ = api
    order = {"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95,
             "take_profits": [{"pct": 2, "close_pct": 50}, {"price": 104, "close_pct": 50}],
             "break_even": {"trigger_tp": 1}}
    r = (await client.post("/v1/orders", json=order)).json()
    assert r["status"] == "executed" and [lv["price"] for lv in r["result"]["take_profits"]] == [102, 104]
    pid = r["result"]["position_id"]
    ok = await client.post(f"/v1/positions/{pid}/take-profits", json={"levels": [{"pct": 3, "close_pct": 100}]})
    assert ok.status_code == 200 and ok.json()["take_profits"][0]["price"] == 103
    assert (await client.delete(f"/v1/positions/{pid}/take-profits")).status_code == 200
    (pos,) = (await client.get("/v1/positions")).json()
    assert "take_profits" not in (pos["management"] or {})
    for bad in ({"levels": []}, {"levels": [{"pct": 2}]}, {"levels": [{"pct": 2, "price": 3, "close_pct": 5}]},
                {"levels": [{"price": 90, "close_pct": 50}]}):
        assert (await client.post(f"/v1/positions/{pid}/take-profits", json=bad)).status_code == 422
    both = {**order, "symbol": "ETHUSDT", "take_profit_price": 110}
    refused = await client.post("/v1/orders", json=both)
    assert refused.status_code == 422 and "detail" in refused.json()     # refused before it is logged


async def test_a_ladder_is_not_replaced_while_an_old_level_cannot_be_cancelled(db):
    class _Sticky(LadderFake):
        async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
            if order_id == "tp1":
                raise RuntimeError("Bybit API error 110001: order not exists or too late to cancel")
            return await super().cancel_order(symbol, order_id, order_link_id, category)
    fake = _Sticky()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    pid = r["position_id"]
    fake.fill("tp1")                                               # it filled just now
    res = await eng.set_take_profits({"id": pid}, [{"pct": 3, "close_pct": 100}])
    assert not res["success"] and res["code"] == "venue_rejected"
    assert len(fake.limits()) == 2                                 # no new ladder placed
    assert [lv["status"] for lv in await _levels(db, pid)] == ["open", "cancelled"]
    assert (await eng.run_stop_rules())["tp_filled"] == [(pid, 1)]  # the next pass records the fill


async def test_cleanup_settles_orders_already_gone_from_the_venue(db):
    class _Gone(LadderFake):
        async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
            if self.book[order_id]["orderStatus"] == "New":
                raise TimeoutError("venue slow")
            raise RuntimeError("Bybit API error 110001: order not exists or too late to cancel")
    fake = _Gone()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    fake.fill("tp1")
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1 WHERE id = $2",
                        datetime.now(UTC), r["position_id"])
    await eng.run_stop_rules()
    # tp1 filled -> gone; tp2 still rests on the venue and the cancel failed -> retried next pass
    assert [lv["status"] for lv in await _levels(db, r["position_id"])] == ["gone", "open"]


async def test_cleanup_takes_not_exists_as_gone_even_before_the_venue_shows_it(db):
    class _AutoCancelled(LadderFake):                    # Bybit cancelled them on the close
        async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
            raise RuntimeError("Bybit API error 110001: order not exists or too late to cancel")

        async def get_order(self, symbol, **kw):
            raise TimeoutError("venue slow")
    fake = _AutoCancelled()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=LADDER)))["position_id"]
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1 WHERE id = $2", datetime.now(UTC), pid)
    assert (await eng.execute_entry(_sig()))["success"]           # no take_profit_leftover wait
    assert [lv["status"] for lv in await _levels(db, pid)] == ["gone", "gone"]


async def test_a_live_short_ladder_buys_back_below_entry(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    await eng.execute_entry(_sig(direction="short", stop_price=105.0, take_profits=LADDER))
    a, b = fake.limits()
    assert (a["side"], a["price"], b["price"]) == ("Buy", "98.0", "96.0")


async def test_a_paper_short_ladder_fills_only_below_entry(db):
    paper = LadderFake()
    eng, _ = await _engine(db, paper)
    r = await eng.execute_entry(_sig(direction="short", stop_price=105.0, take_profits=LADDER))
    paper.price = 101.0                                            # against the short: nothing fills
    assert (await eng.run_stop_rules())["tp_filled"] == []
    paper.price = 97.5
    assert (await eng.run_stop_rules())["tp_filled"] == [(r["position_id"], 1)]


async def test_two_levels_filling_in_one_pass_are_counted_once(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    fake.fill("tp1")
    fake.fill("tp2")
    assert (await eng.run_stop_rules())["tp_filled"] == [(r["position_id"], 1), (r["position_id"], 2)]
    partials = await _rows(db, "SELECT qty FROM trades WHERE position_id = $1 AND trade_type = 'partial_exit'",
                           r["position_id"])
    assert [t["qty"] for t in partials] == [2.5]                   # the second took the rest: the venue closed it


# ---- review round (wave 1) ---------------------------------------------------------------------

async def test_break_even_never_loosens_a_stop_moved_after_the_pass_read_the_position(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng)
    await eng.set_break_even(pos, trigger_pct=2.0)
    fake.price = 110.0
    snapshot = await eng._own_open_positions(all_modes=True)        # the pass reads stop 95 ...
    await eng.move_stop(pos, 106.0)                                  # ... then the user moves it to 106

    async def stale(**kw):
        return snapshot
    eng._own_open_positions = stale
    fake.calls.clear()
    out = await eng.run_stop_rules()
    assert out["already_tighter"] == [pos["id"]] and out["moved"] == []
    assert "set_trading_stop" not in fake.names()
    assert (await _stop(db, pos["id"]))[0] == 106.0


async def test_only_tighten_keeps_a_tighter_stop_for_shorts_too(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng, direction="short", stop_price=105.0)
    fake.price = 90.0
    await eng.move_stop(pos, 98.0)
    fake.calls.clear()
    r = await eng.move_stop(pos, 100.0, only_tighten=True)
    assert r["direction"] == "already_tighter" and r["stop_price"] == 98.0 and fake.calls == []
    r = await eng.move_stop(pos, 97.0, only_tighten=True)
    assert r["direction"] == "tightened" and (await _stop(db, pos["id"]))[0] == 97.0


async def test_the_sync_keeps_a_stop_moved_after_it_read_the_venue(db):
    dep, _ = await _setup(db)
    from tests.test_position_tracker import _open as _tracker_open
    pid = await _tracker_open(db, "BTCUSDT", "LONG", dep)

    class _MovedMeanwhile(_FakeBybit):
        async def get_positions(self, **kw):
            rows = await super().get_positions(**kw)
            async with db.acquire() as c:                            # a stop move lands mid-sync
                await c.execute("UPDATE positions SET stop_price = 98, updated_at = $1 WHERE id = $2",
                                datetime.now(UTC) + timedelta(seconds=1), pid)
            return rows
    await PositionTracker(db, _MovedMeanwhile([_pos("BTCUSDT", "Buy", 1, stop="95")])).sync_all_positions()
    assert (await _stop(db, pid))[0] == 98.0                       # not the venue's older 95
    await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1, stop="98.5")])).sync_all_positions()
    assert (await _stop(db, pid))[0] == 98.5                       # the next sync reads it again


async def test_api_numbers_must_be_numbers(api):  # noqa: F811
    client, _, _ = api
    order = {"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95}
    for bad in ({"stop_price": True}, {"size": {"notional_usd": True}}, {"stop_price": "95"},
                {"trailing": {"distance_pct": True}}, {"break_even": {"trigger_tp": True}},
                {"break_even": {"trigger_tp": 1.0}}, {"take_profits": [{"pct": True, "close_pct": 50}]}):
        assert (await client.post("/v1/orders", json={**order, **bad})).status_code == 422, bad
    pid = (await client.post("/v1/orders", json=order)).json()["result"]["position_id"]
    assert (await client.post(f"/v1/positions/{pid}/stop", json={"stop_price": True})).status_code == 422
    assert (await client.post(f"/v1/positions/{pid}/trailing", json={"distance_pct": True})).status_code == 422
    assert (await client.post(f"/v1/positions/{pid}/break-even", json={"trigger_tp": True})).status_code == 422
    assert (await client.post(f"/v1/positions/{pid}/stop", json={"stop_price": 97})).status_code == 200


async def test_an_order_can_turn_off_the_deployments_rules(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False, settings={
        **SETTINGS, "trailing_distance_pct": 1.5, "take_profit_ladder": LADDER, "break_even_trigger_tp": 1})
    r = await eng.execute_entry(_sig(trailing={}, break_even={}, take_profits=[]))
    assert r["success"], r
    assert (r["trailing"], r["break_even"], r["take_profits"]) == (None, None, None)
    assert not _trailing_calls(fake) and fake.limits() == []
    tp_calls = [c for n, c in fake.calls if n == "set_trading_stop" and c.get("take_profit")]
    assert tp_calls == []                                           # and no deployment take_profit_pct


async def test_an_empty_order_ladder_also_turns_off_the_deployments_single_take_profit(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "take_profit_pct": 3})
    assert (await eng.execute_entry(_sig(take_profits=[])))["success"]
    assert [c for n, c in fake.calls if n == "set_trading_stop" and c.get("take_profit")] == []


async def test_api_false_turns_a_rule_off_for_the_order(api):  # noqa: F811
    client, _, rt = api
    seen = []
    real = rt.engine.execute_entry

    async def spy(signal):
        seen.append(signal)
        return await real(signal)
    rt.engine.execute_entry = spy
    r = await client.post("/v1/orders", json={"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500},
                                              "stop_price": 95, "trailing": False, "break_even": False,
                                              "take_profits": False, "take_profit_price": 110})
    assert r.status_code == 200, r.json()
    assert (seen[0]["trailing"], seen[0]["break_even"], seen[0]["take_profits"]) == ({}, {}, [])
    for bad in ({"take_profits": []}, {"trailing": True}, {"break_even": True}):
        body = {"symbol": "ETHUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95, **bad}
        r = await client.post("/v1/orders", json=body)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list), (bad, r.json())  # the request itself


async def test_prices_derived_below_zero_are_refused(db):
    from relay.execution.engine import break_even_levels
    assert "above 0" in ladder_price_problem("short", 100.0, [{"pct": 100, "close_pct": 50}])
    assert ladder_price_problem("short", 100.0, [{"pct": 99, "close_pct": 50}]) is None
    with pytest.raises(ValueError, match="above 0"):
        break_even_levels("short", 100.0, 105.0, {"trigger_pct": 100}, None)
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    pos = await _open(eng, direction="short", stop_price=105.0)
    fake.calls.clear()
    r = await eng.set_trailing(pos, 1.0, activate_pct=100)
    assert r["code"] == "invalid" and "above 0" in r["error"] and not _trailing_calls(fake)


async def test_one_stop_rules_pass_at_a_time(db):
    import asyncio
    eng, _ = await _engine(db, FakeBybit())
    running, peak = 0, 0

    async def pass_():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {}
    eng._run_stop_rules = pass_
    await asyncio.gather(eng.run_stop_rules(), eng.run_stop_rules())
    assert peak == 1


THREE = [{"pct": 2, "close_pct": 20}, {"pct": 4, "close_pct": 20}, {"pct": 6, "close_pct": 20}]


async def _partials(db, pid):
    return await _rows(db, "SELECT qty, exit_reason FROM trades WHERE position_id = $1 "
                           "AND trade_type = 'partial_exit' ORDER BY id", pid)


async def test_a_fill_is_recorded_once_even_when_the_rest_of_the_pass_fails(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=THREE)))["position_id"]
    fake.fill("tp1")
    real_fit = eng._fit_ladder_to_size

    async def broken(*a, **kw):
        raise RuntimeError("instrument specs unreadable")
    eng._fit_ladder_to_size = broken
    assert (await eng.run_stop_rules())["refused"][0]["code"] == "error"
    eng._fit_ladder_to_size = real_fit
    await eng.run_stop_rules()
    await eng.run_stop_rules()
    assert [t["qty"] for t in await _partials(db, pid)] == [1.0]
    (p,) = await _rows(db, "SELECT size FROM positions WHERE id = $1", pid)
    assert p["size"] == 4.0


async def test_an_unreadable_level_is_retried_without_recording_the_others_twice(db):
    class _Flaky(LadderFake):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.fail = {"tp2"}

        async def get_order(self, symbol, order_id=None, **kw):
            if order_id in self.fail:
                self.fail.discard(order_id)
                raise TimeoutError("venue slow")
            return await super().get_order(symbol, order_id=order_id, **kw)
    fake = _Flaky()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=THREE)))["position_id"]
    fake.fill("tp1")
    fake.fill("tp2")
    out = await eng.run_stop_rules()
    assert out["tp_filled"] == [(pid, 1)] and out["refused"] == []  # an unreadable order is not a failure
    assert (await eng.run_stop_rules())["tp_filled"] == [(pid, 2)]
    assert [t["qty"] for t in await _partials(db, pid)] == [1.0, 1.0]
    assert [lv["status"] for lv in await _levels(db, pid)] == ["filled", "filled", "open"]


async def test_no_partial_close_is_recorded_on_a_closed_position(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSED' WHERE id = $1", pos["id"])
        (row,) = await c.fetch("SELECT * FROM positions WHERE id = $1", pos["id"])
        recorded, pnl = await eng._record_partial_tx(c, dict(row), 2.0, 101.0, None, "take_profit", True)
    assert (recorded, pnl) == (False, None)
    assert await _partials(db, pos["id"]) == []


async def test_replacing_a_ladder_records_what_part_filled_before_the_cancel(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=LADDER)))["position_id"]
    fake.fill("tp1", qty=1.0, status="PartiallyFilled")             # still resting, 1 of 2.5 filled
    r = await eng.set_take_profits({"id": pid}, [{"pct": 3, "close_pct": 100}])
    assert r["success"], r
    assert [t["qty"] for t in await _partials(db, pid)] == [1.0]
    (p,) = await _rows(db, "SELECT size FROM positions WHERE id = $1", pid)
    assert p["size"] == 4.0
    assert r["take_profits"][0]["qty"] == 4.0                       # sized on what is left
    await eng.run_stop_rules()
    assert [t["qty"] for t in await _partials(db, pid)] == [1.0]   # never again


async def test_shrinking_a_ladder_records_what_part_filled_before_the_cancel(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=LADDER)))["position_id"]
    fake.fill("tp2", qty=0.5, status="PartiallyFilled")
    async with db.acquire() as c:                                    # reduced by hand to 2.5
        await c.execute("UPDATE positions SET size = 2.5 WHERE id = $1", pid)
    await eng.run_stop_rules()
    assert [t["qty"] for t in await _partials(db, pid)] == [0.5]
    levels = await _levels(db, pid)
    assert levels[1]["status"] == "cancelled" and levels[1]["filled_qty"] == 0.5
    assert "unrecorded" not in levels[1]
    assert levels[0]["qty"] == 2.0                                  # fits the 2.0 left, counting the part-fill


class _LinkFake(LadderFake):
    """The venue takes the order but the answer is lost (a timeout)."""
    history_down = False

    async def place_limit_order(self, **kw):
        await super().place_limit_order(**kw)
        self.book[f"tp{len(self.book)}"]["orderLinkId"] = kw["order_link_id"]
        raise TimeoutError("read timed out")

    async def get_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        if order_link_id and self.history_down:
            raise TimeoutError("venue slow")
        return await super().get_order(symbol, order_id=order_id, order_link_id=order_link_id)

    async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        self._rec("cancel_order", symbol=symbol, order_id=order_id, order_link_id=order_link_id)
        for o in self.book.values():
            if order_id == o["orderId"] or (order_link_id and o.get("orderLinkId") == order_link_id):
                o["orderStatus"] = "Cancelled"
        return {}


async def test_a_level_placed_despite_a_timeout_is_tracked(db):
    fake = _LinkFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    assert [(lv["status"], lv["order_id"]) for lv in r["take_profits"]] == [("open", "tp1"), ("open", "tp2")]


async def test_a_level_unreadable_after_a_timeout_is_cancelled_by_its_link(db):
    fake = _LinkFake()
    fake.history_down = True
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(take_profits=LADDER))
    assert [lv["status"] for lv in r["take_profits"]] == ["failed", "failed"]
    cancels = [c["order_link_id"] for n, c in fake.calls if n == "cancel_order"]
    assert cancels == [lv["order_link_id"] for lv in r["take_profits"]]
    assert all(o["orderStatus"] == "Cancelled" for o in fake.book.values())


async def test_a_live_entry_waits_while_an_earlier_ladder_may_still_rest(db):
    class _Stuck(LadderFake):
        async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
            raise RuntimeError("venue busy")
    fake = _Stuck()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=LADDER)))["position_id"]
    async with db.acquire() as c:                                    # the stop closed it on the venue
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1 WHERE id = $2",
                        datetime.now(UTC), pid)
    fake.calls.clear()
    r = await eng.execute_entry(_sig())
    assert r == {"success": False, "reason": "take_profit_leftover"}
    assert "place_market_order" not in fake.names()
    for o in fake.book.values():                                     # gone on the venue after all
        o["orderStatus"] = "Cancelled"
    assert (await eng.execute_entry(_sig()))["success"]


async def test_a_reclose_retires_the_ladder_so_its_fill_is_never_counted_twice(db):
    fake = LadderFake()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=LADDER)))["position_id"]
    fake.fill("tp1")                                                 # 2.5 closed by the ladder ...
    async with db.acquire() as c:                                    # ... while a close was unconfirmed
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", pid)
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "2.5", "avgPrice": "100"}]

    async def failing_close(**kw):
        return {"success": False, "error": "venue busy"}
    eng.order_executor.close_position = failing_close
    with pytest.raises(Exception, match="venue busy"):
        await eng.execute_exit({"id": pid}, reason="manual")
    async with db.acquire() as c:                                    # the sync: still open, back to OPEN
        await c.execute("UPDATE positions SET status = 'OPEN' WHERE id = $1", pid)
    await eng.run_stop_rules()
    assert [(t["qty"], t["exit_reason"]) for t in await _partials(db, pid)] == [(2.5, "closed_on_venue")]
    assert all(lv["status"] != "open" for lv in await _levels(db, pid))
    assert fake.book["tp2"]["orderStatus"] == "Cancelled"


async def test_an_older_fill_the_real_time_view_no_longer_lists_is_read_from_the_history(db):
    class _Aged(LadderFake):
        async def get_order(self, symbol, **kw):
            return None

        async def get_order_history(self, **kw):
            if kw.get("order_id") in self.book:
                return [self.book[kw["order_id"]]]
            return await super().get_order_history(**kw)
    fake = _Aged()
    eng, _ = await _engine(db, fake, dry_run=False)
    pid = (await eng.execute_entry(_sig(take_profits=LADDER)))["position_id"]
    fake.fill("tp1")
    assert (await eng.run_stop_rules())["tp_filled"] == [(pid, 1)]


# ---- the time stop ---------------------------------------------------------------------------

async def _age(db, pid, hours):
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET opened_at = $1 WHERE id = $2",
                        datetime.now(UTC) - timedelta(hours=hours), pid)


async def test_a_position_past_the_time_stop_is_closed_and_a_younger_one_stays(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "max_hold_hours": 4.0})
    old, young = await _open(eng), await _open(eng, symbol="ETHUSDT")
    await _age(db, old["id"], 4.1)
    await _age(db, young["id"], 3.9)
    r = await eng.run_stop_rules()
    assert r["time_stopped"] == [old["id"]]
    status = {row["id"]: row["status"] for row in await _rows(db, "SELECT id, status FROM positions")}
    assert status == {old["id"]: "CLOSED", young["id"]: "OPEN"}
    (t,) = await _rows(db, "SELECT exit_reason FROM trades WHERE position_id = $1 AND trade_type = 'exit'", old["id"])
    assert t["exit_reason"] == "time_stop"
    assert (await eng.run_stop_rules())["time_stopped"] == []          # closed once


async def test_no_time_stop_when_it_is_empty(db):
    eng, _ = await _engine(db, FakeBybit())
    pos = await _open(eng)
    await _age(db, pos["id"], 10_000)
    assert (await eng.run_stop_rules())["time_stopped"] == []
    (p,) = await _rows(db, "SELECT status FROM positions")
    assert p["status"] == "OPEN"


async def test_a_live_time_stop_sends_a_reduce_only_close(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "max_hold_hours": 1.0})
    pos = await _open(eng)
    await _age(db, pos["id"], 2)
    fake.calls.clear()
    assert (await eng.run_stop_rules())["time_stopped"] == [pos["id"]]
    assert [c for n, c in fake.calls if n == "place_market_order" and c.get("reduce_only")]


async def test_a_time_stop_close_that_fails_is_retried_and_reported_once(db):
    from relay.events import EventLog

    class _NoClose(FakeBybit):
        fail = True

        async def place_market_order(self, **kw):
            if kw.get("reduce_only") and self.fail:
                raise RuntimeError("Bybit API error: rate limited")
            return await super().place_market_order(**kw)

    fake = _NoClose()
    eng, dep = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "max_hold_hours": 1.0})
    eng.events = EventLog(db, dep)
    pos = await _open(eng)
    await _age(db, pos["id"], 2)
    for _ in range(2):
        r = await eng.run_stop_rules()
        assert r["time_stopped"] == [] and r["refused"][0]["code"] == "time_stop_failed"
    events = await _rows(db, "SELECT severity FROM events WHERE kind = 'position.time_stop_failed'")
    assert [e["severity"] for e in events] == ["critical"]
    fake.fail = False
    assert (await eng.run_stop_rules())["time_stopped"] == [pos["id"]]
