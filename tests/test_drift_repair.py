"""Size drift between a live record and the venue: alerted, then repaired on the next pass."""
import pytest

from relay.events import EventLog
from tests.test_api import api  # noqa: F401  (fixture)
from tests.test_engine import FakeBybit, _engine, _no_sleep, _rows, _sig  # noqa: F401


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _live(db, fake=None):
    fake = fake or FakeBybit()
    eng, dep = await _engine(db, fake, dry_run=False)
    eng.events = EventLog(db, dep)
    eng._clock = _Clock()
    pid = (await eng.execute_entry(_sig()))["position_id"]              # 5 @ 100
    return eng, fake, pid


def _venue(fake, size, avg="100", side="Buy"):
    fake.positions = [{"symbol": "BTCUSDT", "side": side, "size": str(size), "avgPrice": avg}]


async def _sync(eng, wait=60):
    """One position-sync pass, `wait` seconds after the last (the default sync interval)."""
    eng._clock.t += wait
    return await eng.repair_drift()


async def _row(db, pid):
    (r,) = await _rows(db, "SELECT size, entry_price, status, entry_meta FROM positions WHERE id = $1", pid)
    return r


async def _events(db):
    return [(r["kind"], r["severity"]) for r in await _rows(db, "SELECT kind, severity FROM events ORDER BY id")]


async def test_no_drift_no_alert(db):
    eng, fake, pid = await _live(db)
    _venue(fake, 5)
    assert await _sync(eng) == {"detected": [], "repaired": [], "unrepairable": []}
    assert await _events(db) == []


async def test_a_manual_reduce_on_the_venue_is_alerted_then_recorded_as_a_partial(db):
    eng, fake, pid = await _live(db)
    _venue(fake, 3)                                                    # 2 closed by hand on the venue
    assert (await _sync(eng))["detected"] == [pid]
    assert (await _row(db, pid))["size"] == 5.0                        # first sighting: alert only
    assert await _events(db) == [("position.drift_detected", "warning")]
    assert (await _sync(eng))["repaired"] == [pid]
    assert (await _row(db, pid))["size"] == 3.0
    (t,) = await _rows(db, "SELECT qty, price, exit_reason FROM trades WHERE position_id = $1 "
                           "AND trade_type = 'partial_exit'", pid)
    assert (t["qty"], t["price"], t["exit_reason"]) == (2.0, None, "reduced_on_venue")   # priced by the venue
    assert await _events(db) == [("position.drift_detected", "warning"), ("position.drift_repaired", "warning")]
    assert await _sync(eng) == {"detected": [], "repaired": [], "unrepairable": []}


async def test_an_add_on_the_venue_takes_the_venues_size_and_average(db):
    eng, fake, pid = await _live(db)
    _venue(fake, 8, avg="110")                                         # 3 added at 126.67 on the venue
    await _sync(eng)
    assert (await _sync(eng))["repaired"] == [pid]
    row = await _row(db, pid)
    assert (row["size"], row["entry_price"]) == (8.0, 110.0)
    entries = await _rows(db, "SELECT qty, price, metadata FROM trades WHERE position_id = $1 "
                              "AND trade_type = 'entry' ORDER BY id", pid)
    add = entries[-1]
    assert "added_on_venue" in str(add["metadata"])
    assert add["qty"] == 3.0 and add["price"] == pytest.approx((110 * 8 - 100 * 5) / 3)


async def test_a_difference_that_changes_between_passes_is_not_repaired(db):
    eng, fake, pid = await _live(db)
    _venue(fake, 4.9)                                                  # e.g. a take-profit fill in flight
    await _sync(eng)
    _venue(fake, 4)
    assert (await _sync(eng))["detected"] == [pid]            # a new difference: alerted again
    assert (await _row(db, pid))["size"] == 5.0
    _venue(fake, 5)                                                    # the record caught up
    assert await _sync(eng) == {"detected": [], "repaired": [], "unrepairable": []}
    _venue(fake, 4)
    assert (await _sync(eng))["detected"] == [pid]            # resolved before: starts over


async def test_a_position_the_venue_no_longer_holds_is_a_close_not_drift(db):
    eng, fake, pid = await _live(db)
    fake.positions = []
    await _sync(eng)
    assert await _sync(eng) == {"detected": [], "repaired": [], "unrepairable": []}
    assert (await _row(db, pid))["size"] == 5.0


async def test_a_shared_venue_position_is_alerted_once_and_never_repaired(db):
    from datetime import UTC, datetime
    eng, fake, pid = await _live(db)
    async with db.acquire() as c:                                    # an imported record on the same side
        await c.execute("INSERT INTO positions (symbol, side, size, entry_price, status, dry_run, opened_at, "
                        "created_at, updated_at) VALUES ('BTCUSDT', 'LONG', 1, 100, 'OPEN', false, $1, $1, $1)",
                        datetime.now(UTC))
    _venue(fake, 7)
    assert (await _sync(eng))["unrepairable"] == [pid]
    assert (await _sync(eng))["unrepairable"] == []           # once per difference
    assert (await _row(db, pid))["size"] == 5.0
    assert await _events(db) == [("position.drift_unrepairable", "critical")]


async def test_a_change_between_the_check_and_the_repair_waits(db):
    class _Moving(FakeBybit):
        calls_seen = 0

        async def get_positions(self, category="linear", symbol=None, settle_coin=None):
            if symbol is not None:                                     # the re-read under the lock
                return [{"symbol": "BTCUSDT", "side": "Buy", "size": "2", "avgPrice": "100"}]
            return self.positions
    eng, fake, pid = await _live(db, _Moving())
    _venue(fake, 3)
    await _sync(eng)
    assert (await _sync(eng))["repaired"] == []
    assert (await _row(db, pid))["size"] == 5.0


async def test_a_close_in_progress_is_left_alone(db):
    eng, fake, pid = await _live(db)
    _venue(fake, 3)
    eng._own_close(pid)
    for _ in range(2):
        assert await _sync(eng) == {"detected": [], "repaired": [], "unrepairable": []}
    assert await _events(db) == []


async def test_paper_positions_never_drift(db):
    fake = FakeBybit()
    paper, _ = await _engine(db, fake)
    assert (await paper.execute_entry(_sig()))["success"]
    _venue(fake, 3)                                                    # the venue holds something else
    await paper.repair_drift()
    assert await paper.repair_drift() == {"detected": [], "repaired": [], "unrepairable": []}


async def test_events_are_listed_by_the_api(api):  # noqa: F811
    client, _, rt = api
    assert rt.engine.events is rt.events                               # the engine reports to the same log
    await rt.events.record("position.drift_detected", "warning", "BTCUSDT long: the record says 5, the venue 3",
                           {"recorded": 5, "venue": 3}, 7)
    (e,) = (await client.get("/v1/events?kind=position.drift_detected")).json()
    assert (e["kind"], e["severity"], e["position_id"], e["data"]["venue"]) == ("position.drift_detected",
                                                                                "warning", 7, 3)
    assert (await client.get("/v1/events?kind=other")).json() == []
    kinds = [x["kind"] for x in (await client.get("/v1/events")).json()]
    assert kinds == ["position.drift_detected", "relay.started"]                 # newest first


async def test_the_position_sync_loop_runs_the_drift_check(db):
    import asyncio
    eng, _ = await _engine(db, FakeBybit())
    ran = asyncio.Event()

    async def check():
        ran.set()
        return {"detected": [], "repaired": [], "unrepairable": []}
    eng.repair_drift = check
    await eng.start()
    try:
        await asyncio.wait_for(ran.wait(), 2)
    finally:
        await eng.stop()


class _Ladder(FakeBybit):
    """A venue whose take-profit order tp1 has filled `tp_filled` and is still resting (or done)."""

    def __init__(self, tp_filled=0.0, tp_status="PartiallyFilled", readable=True):
        super().__init__()
        self.tp_filled, self.tp_status, self.readable = tp_filled, tp_status, readable

    async def get_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        if order_id == "tp1":
            if not self.readable:
                raise RuntimeError("timeout")
            return {"orderId": "tp1", "orderStatus": self.tp_status, "cumExecQty": str(self.tp_filled),
                    "avgPrice": "110"}
        return None


async def _with_ladder(db, pid, qty=4):
    import json
    levels = [{"n": 1, "status": "open", "order_id": "tp1", "qty": qty, "price": 110.0}]
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET management = $1 WHERE id = $2",
                        json.dumps({"take_profits": {"levels": levels}}), pid)


async def test_a_take_profit_partly_filled_and_resting_is_not_drift(db):
    fake = _Ladder(tp_filled=2)
    eng, fake, pid = await _live(db, fake)
    await _with_ladder(db, pid)
    _venue(fake, 3)                                                    # 5 less the 2 the ladder filled
    for _ in range(3):
        assert await _sync(eng) == {"detected": [], "repaired": [], "unrepairable": []}
    assert (await _row(db, pid))["size"] == 5.0 and await _events(db) == []


async def test_a_manual_reduce_beside_a_resting_take_profit_counts_only_the_manual_part(db):
    fake = _Ladder(tp_filled=2)
    eng, fake, pid = await _live(db, fake)
    await _with_ladder(db, pid)
    _venue(fake, 2)                                                    # 2 by the ladder, 1 by hand
    assert (await _sync(eng))["detected"] == [pid]
    assert (await _sync(eng))["repaired"] == [pid]
    assert (await _row(db, pid))["size"] == 4.0                        # the ladder still records its 2
    (t,) = await _rows(db, "SELECT qty FROM trades WHERE position_id = $1 AND trade_type = 'partial_exit'", pid)
    assert t["qty"] == 1.0


async def test_an_add_beside_a_resting_take_profit_keeps_the_ladders_part_to_come(db):
    fake = _Ladder(tp_filled=2)
    eng, fake, pid = await _live(db, fake)
    await _with_ladder(db, pid)
    _venue(fake, 6, avg="104")                                         # 5 - 2 + 3 added by hand
    await _sync(eng)
    assert (await _sync(eng))["repaired"] == [pid]
    row = await _row(db, pid)
    assert (row["size"], row["entry_price"]) == (8.0, 104.0)
    entries = await _rows(db, "SELECT qty, price, metadata FROM trades WHERE position_id = $1 "
                              "AND trade_type = 'entry' ORDER BY id", pid)
    (add,) = [e for e in entries if "added_on_venue" in str(e["metadata"])]
    assert add["qty"] == 3.0 and add["price"] == pytest.approx((104 * 8 - 100 * 5) / 3)


async def test_an_unreadable_ladder_order_leaves_the_position_for_the_next_pass(db):
    fake = _Ladder(tp_filled=2)
    eng, fake, pid = await _live(db, fake)
    await _with_ladder(db, pid)
    _venue(fake, 2)
    assert (await _sync(eng))["detected"] == [pid]
    fake.readable = False
    assert await _sync(eng) == {"detected": [], "repaired": [], "unrepairable": []}
    fake.readable = True
    assert (await _sync(eng))["repaired"] == [pid]                     # still the same difference: repaired


async def test_a_ladder_fill_between_check_and_repair_waits(db):
    class _Filling(_Ladder):
        async def get_positions(self, category="linear", symbol=None, settle_coin=None):
            if symbol is not None:
                self.tp_filled = 2.5                                   # the ladder moved meanwhile
            return self.positions
    fake = _Filling(tp_filled=2)
    eng, fake, pid = await _live(db, fake)
    await _with_ladder(db, pid)
    _venue(fake, 2)
    await _sync(eng)
    assert (await _sync(eng))["repaired"] == []
    assert (await _row(db, pid))["size"] == 5.0


async def test_a_difference_is_repaired_only_once_it_is_old_enough(db):
    from relay.execution.engine import DRIFT_MIN_AGE_RULE_PASSES
    eng, fake, pid = await _live(db)
    min_age = DRIFT_MIN_AGE_RULE_PASSES * 10                           # the stop-rules interval (default 10 s)
    _venue(fake, 3)
    assert (await _sync(eng, wait=1))["detected"] == [pid]
    assert (await _sync(eng, wait=1))["repaired"] == []                # a fast sync: too soon
    assert (await _sync(eng, wait=min_age - 1.5))["repaired"] == []
    assert (await _sync(eng, wait=0.5))["repaired"] == [pid]


async def test_one_position_that_fails_does_not_stop_the_others(db):
    class _BySymbol(FakeBybit):
        async def get_positions(self, category="linear", symbol=None, settle_coin=None):
            return [p for p in self.positions if symbol is None or p["symbol"] == symbol]
    eng, fake, pid = await _live(db, _BySymbol())
    eth = (await eng.execute_entry(_sig(symbol="ETHUSDT")))["position_id"]
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "3", "avgPrice": "100"},
                      {"symbol": "ETHUSDT", "side": "Buy", "size": "3", "avgPrice": "100"}]
    real = eng._shares_venue_position

    async def broken(p):
        if p["symbol"] == "BTCUSDT":
            raise RuntimeError("database hiccup")
        return await real(p)
    eng._shares_venue_position = broken
    assert (await _sync(eng))["detected"] == [eth]
    assert (await _sync(eng))["repaired"] == [eth]
    eng._shares_venue_position = real
    assert (await _sync(eng))["detected"] == [pid]


async def test_an_unrepairable_difference_that_resolves_and_returns_is_alerted_again(db):
    from datetime import UTC, datetime
    eng, fake, pid = await _live(db)
    async with db.acquire() as c:
        await c.execute("INSERT INTO positions (symbol, side, size, entry_price, status, dry_run, opened_at, "
                        "created_at, updated_at) VALUES ('BTCUSDT', 'LONG', 1, 100, 'OPEN', false, $1, $1, $1)",
                        datetime.now(UTC))
    _venue(fake, 7)
    assert (await _sync(eng))["unrepairable"] == [pid]
    _venue(fake, 5)                                                    # someone fixed it
    await _sync(eng)
    _venue(fake, 7)
    assert (await _sync(eng))["unrepairable"] == [pid]
