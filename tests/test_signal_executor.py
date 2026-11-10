"""Event executor: v1 event signals carried out on the engine, outcomes recorded on the signal row."""
import uuid
from datetime import UTC, datetime

import pytest

from relay.signals.contract import parse_signal
from relay.signals.executor import EventExecutor, bybit_symbol
from relay.signals.log import SignalLog
from tests.test_engine import SETTINGS, FakeBybit, _engine, _no_sleep, _rows  # noqa: F401  (autouse)

ENV = {"version": "1", "source": "mybot", "deployment": "default", "sent_at": "2026-11-02T08:00:00Z",
       "kind": "event"}
OPEN = {"action": "open", "symbol": "BTC/USDT:USDT", "side": "long", "size": {"notional_usd": 500},
        "stop": {"price": 95}}


async def _run(eng, body):
    log = eng.signal_log
    sig = parse_signal({**ENV, "id": uuid.uuid4().hex, **body})
    row, _ = await log.record(sig.source, sig.id, body, deployment="default", kind="event")
    res = await EventExecutor(eng, log).execute(sig, row.id)
    return res, await log.get(row.id)


async def _eng(db, fake=None, dry_run=False, settings=None):
    eng, _ = await _engine(db, fake or FakeBybit(), dry_run=dry_run, settings=settings)
    eng.signal_log = SignalLog(db)
    return eng


async def _positions(db):
    return await _rows(db, "SELECT id, side, status, size, dry_run, initial_stop_price, stop_price FROM positions "
                           "ORDER BY id")


def test_bybit_symbols():
    assert bybit_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert bybit_symbol("1000PEPE/USDT:USDT") == "1000PEPEUSDT"
    with pytest.raises(ValueError, match="only USDT-settled"):
        bybit_symbol("ETH/USDC:USDC")


async def test_open_executes_and_records_the_result(db):
    eng = await _eng(db)
    res, row = await _run(eng, OPEN)
    assert res["success"] and row.status == "executed"
    assert row.result["position_id"] == res["position_id"] and row.result["action"] == "open"
    (p,) = await _positions(db)
    assert (p["side"], p["status"], p["dry_run"], p["initial_stop_price"]) == ("LONG", "OPEN", False, 95.0)


@pytest.mark.parametrize("side, stop", [("long", 98.0), ("short", 102.0)])
async def test_a_stop_as_a_percentage_from_the_entry(db, side, stop):
    eng = await _eng(db)
    res, _ = await _run(eng, {**OPEN, "side": side, "stop": {"pct": 2}})
    assert res["success"], res
    (p,) = await _positions(db)
    assert p["initial_stop_price"] == pytest.approx(stop)


async def test_an_open_the_entry_checks_refuse_keeps_their_verdict(db):
    eng = await _eng(db)
    res, row = await _run(eng, {k: v for k, v in OPEN.items() if k != "stop"})
    assert res["reason"] == "stop_required"
    assert (row.status, row.reasons, row.result["reason"]) == ("skipped", ["stop_required"], "stop_required")


async def test_a_dry_run_signal_opens_a_paper_position_on_a_live_deployment(db):
    fake = FakeBybit()
    eng = await _eng(db, fake)
    res, row = await _dry(eng, OPEN)
    assert res["success"] and res["dry_run"] is True and row.status == "executed"
    assert "place_market_order" not in fake.names() and "set_trading_stop" not in fake.names()
    (p,) = await _positions(db)
    assert bool(p["dry_run"])
    res, _ = await _run(eng, OPEN)                                    # the live one is separate
    assert res["success"] and res["dry_run"] is False


async def _dry(eng, body):
    full = {**ENV, "id": uuid.uuid4().hex, **body, "dry_run": True}
    s = parse_signal(full)
    r, _ = await eng.signal_log.record(s.source, s.id, full)
    return await EventExecutor(eng, eng.signal_log).execute(s, r.id), await eng.signal_log.get(r.id)


async def test_a_dry_run_signal_never_touches_a_live_position(db):
    fake = FakeBybit()
    eng = await _eng(db, fake)
    await _run(eng, OPEN)
    fake.calls.clear()
    for body in ({"action": "close", "symbol": "BTCUSDT"}, {"action": "reduce", "symbol": "BTCUSDT",
                                                          "size": {"close_pct": 50}},
                 {"action": "move_stop", "symbol": "BTCUSDT", "stop": {"price": 97}}):
        res, row = await _dry(eng, body)
        assert (res["reason"], row.status) == ("no_position", "skipped"), body
    res, _ = await _dry(eng, {"action": "close_all"})
    assert res == {"success": True, "closed": 0, "note": "nothing open (paper)"}
    assert fake.calls == []
    (p,) = await _positions(db)
    assert (p["status"], p["size"], p["stop_price"]) == ("OPEN", 5.0, 95.0)


async def test_close_reduce_and_move_stop(db):
    eng = await _eng(db)
    pid = (await _run(eng, OPEN))[0]["position_id"]
    res, row = await _run(eng, {"action": "move_stop", "symbol": "BTCUSDT", "stop": {"price": 97}})
    assert (res["stop_price"], res["position_id"], row.status) == (97, pid, "executed")
    res, row = await _run(eng, {"action": "reduce", "symbol": "BTCUSDT", "size": {"close_pct": 40}})
    assert res["success"] and row.status == "executed"
    assert (await _positions(db))[0]["size"] == pytest.approx(3.0)
    res, row = await _run(eng, {"action": "close", "symbol": "BTCUSDT", "side": "long"})
    assert res == {"success": True, "position_id": pid, "close": "closed"} and row.status == "executed"
    assert (await _positions(db))[0]["status"] == "CLOSED"


async def test_refusals_are_skipped_with_their_reason(db):
    eng = await _eng(db)
    res, row = await _run(eng, {"action": "close", "symbol": "BTCUSDT"})
    assert (res["reason"], row.status, row.reasons) == ("no_position", "skipped", ["no_position"])
    await _run(eng, OPEN)
    res, _ = await _run(eng, {"action": "close", "symbol": "BTCUSDT", "side": "short"})
    assert res["reason"] == "no_position"
    res, row = await _run(eng, {"action": "move_stop", "symbol": "BTCUSDT", "stop": {"price": 101}})
    assert (res["reason"], row.status) == ("stop_already_breached", "skipped")
    res, row = await _run(eng, {"action": "reduce", "symbol": "BTCUSDT", "size": {"close_pct": 0.0001}})
    assert (res["reason"], row.status) == ("reduce_refused", "skipped")
    res, row = await _run(eng, {**OPEN, "symbol": "ETH/USDC:USDC"})
    assert (res["reason"], row.status) == ("unsupported_symbol", "skipped")
    res, row = await _run(eng, {**OPEN, "execution": {"type": "limit", "price": 99}})
    assert (res["reason"], row.status) == ("execution_not_supported", "skipped")


async def test_a_venue_failure_is_failed_not_skipped(db, monkeypatch):
    eng = await _eng(db)
    await _run(eng, OPEN)

    async def broken(position, reason="manual", client=None):
        raise Exception("Failed to close position BTCUSDT: venue down")
    monkeypatch.setattr(eng, "execute_exit", broken)
    res, row = await _run(eng, {"action": "close", "symbol": "BTCUSDT"})
    assert (res["reason"], row.status) == ("close_failed", "failed") and "venue down" in res["error"]
    res, row = await _run(eng, {"action": "close_all"})
    assert (res["success"], res["reason"], row.status, res["closed"]) == (False, "close_failed", "failed", 0)


async def test_an_unexpected_error_is_recorded_as_failed(db, monkeypatch):
    eng = await _eng(db)

    async def broken(all_modes=False):
        raise RuntimeError("database gone")
    monkeypatch.setattr(eng, "_own_open_positions", broken)
    res, row = await _run(eng, {"action": "close", "symbol": "BTCUSDT"})
    assert (res["reason"], row.status) == ("internal_error", "failed")


async def test_flip_closes_the_other_side_then_opens_even_with_a_cooldown(db):
    eng = await _eng(db, settings={**SETTINGS, "cooldown_hours": 4})
    first = (await _run(eng, OPEN))[0]["position_id"]
    res, row = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "short", "size": {"qty": 2},
                                "stop": {"price": 105}})
    assert res["success"] and res["closed_position_ids"] == [first] and row.status == "executed"
    old, new = await _positions(db)
    assert (old["status"], new["side"], new["status"], new["size"]) == ("CLOSED", "SHORT", "OPEN", 2.0)
    res, row = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "short", "size": {"qty": 2},
                                "stop": {"price": 105}})
    assert (res["reason"], row.status) == ("already_that_side", "skipped")
    await _run(eng, {"action": "close", "symbol": "BTCUSDT"})
    res, _ = await _run(eng, OPEN)                                    # a plain open IS held by the cooldown
    assert res["reason"] == "cooldown"


async def test_flip_with_nothing_open_is_an_open(db):
    eng = await _eng(db)
    res, _ = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "long", "size": {"qty": 1},
                              "stop": {"price": 95}})
    assert res["success"] and res["closed_position_ids"] == []


async def test_a_half_done_flip_is_executed_with_the_reason(db):
    eng = await _eng(db)
    await _run(eng, OPEN)
    res, row = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "short", "size": {"qty": 1},
                                "stop": {"price": 99}})                # stop on the wrong side for a short
    assert row.status == "executed" and row.reasons == ["flip_open_refused: stop_wrong_side"]
    assert [p["status"] for p in await _positions(db)] == ["CLOSED"]


async def test_a_failed_close_stops_the_flip(db, monkeypatch):
    fake = FakeBybit()
    eng = await _eng(db, fake)
    await _run(eng, OPEN)

    async def broken(position, reason="manual", client=None):
        raise Exception("Failed to close position BTCUSDT: venue down")
    monkeypatch.setattr(eng, "execute_exit", broken)
    fake.calls.clear()
    res, row = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "short", "size": {"qty": 1},
                                "stop": {"price": 105}})
    assert (res["reason"], row.status, res["closed_position_ids"]) == ("close_failed", "failed", [])
    assert "place_market_order" not in fake.names()


async def test_close_all_closes_the_signals_mode_only(db):
    eng = await _eng(db)
    await _run(eng, OPEN)
    await _dry(eng, {**OPEN, "symbol": "ETHUSDT"})
    res, row = await _run(eng, {"action": "close_all"})
    assert res == {"success": True, "closed": 1, "errors": []} and row.status == "executed"
    assert [(p["dry_run"], p["status"]) for p in await _positions(db)] == [(False, "CLOSED"), (True, "OPEN")]


async def test_close_acts_on_its_own_symbol_only(db):
    eng = await _eng(db)
    await _run(eng, OPEN)
    eth = (await _run(eng, {**OPEN, "symbol": "ETHUSDT"}))[0]["position_id"]
    res, _ = await _run(eng, {"action": "close", "symbol": "ETH/USDT:USDT"})
    assert res["position_id"] == eth
    assert [p["status"] for p in await _positions(db)] == ["OPEN", "CLOSED"]


async def test_false_turns_off_the_deployments_ladder_for_the_signal(db):
    eng = await _eng(db, settings={**SETTINGS, "take_profit_ladder": [{"pct": 2, "close_pct": 50}]})
    res, _ = await _run(eng, {**OPEN, "take_profit": False})
    assert res["success"] and res["take_profits"] is None
    res, _ = await _run(eng, {**OPEN, "symbol": "ETHUSDT"})
    assert res["take_profits"]                                       # without it: the deployment's


async def test_the_entry_paths_own_verdict_is_kept(db, monkeypatch):
    eng = await _eng(db)

    async def entry(signal):                       # e.g. filled, but the stop had already been passed
        await eng.signal_log.mark_outcome(signal["signal_id"], "stop_breached_at_fill")
        return {"success": False, "reason": "stop_breached_at_fill"}
    monkeypatch.setattr(eng, "execute_entry", entry)
    _, row = await _run(eng, OPEN)
    assert (row.status, row.reasons, row.result["reason"]) == ("executed", ["stop_breached_at_fill"],
                                                                "stop_breached_at_fill")


# ---- review round (wave 2) ---------------------------------------------------------------------

async def test_a_paper_entry_is_checked_against_the_paper_book_only(db):
    eng = await _eng(db, settings={**SETTINGS, "max_positions": 1})
    await _run(eng, OPEN)                                             # a live BTCUSDT position
    res, _ = await _dry(eng, OPEN)                                    # paper: no symbol conflict, own count
    assert res["success"], res
    res, _ = await _dry(eng, {**OPEN, "symbol": "ETHUSDT"})           # the paper book holds 1 already
    assert "Position limit reached" in res["reason"]
    res, _ = await _run(eng, {**OPEN, "symbol": "ETHUSDT"})           # the live book holds 1 already
    assert "Position limit reached" in res["reason"]


async def test_a_paper_entry_makes_no_venue_call_even_to_clean_up(db):
    from tests.test_position_management import LADDER, LadderFake
    fake = LadderFake()
    eng = await _eng(db, fake)
    pid = (await _run(eng, {**OPEN, "take_profit": LADDER}))[0]["position_id"]
    async with db.acquire() as c:                                    # stopped out; its levels still listed
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1 WHERE id = $2",
                        datetime.now(UTC), pid)
    fake.calls.clear()
    res, _ = await _dry(eng, OPEN)
    assert res["success"] and "cancel_order" not in fake.names()


async def test_a_percentage_stop_is_set_from_the_actual_fill(db):
    class _Slipped(FakeBybit):
        async def get_order_history(self, **kw):
            rows = await super().get_order_history(**kw)
            return [{**r, "avgPrice": "101"} for r in rows]          # filled 1% worse than the ticker
    eng = await _eng(db, _Slipped())
    res, _ = await _run(eng, {**OPEN, "stop": {"pct": 2}})
    assert res["success"], res
    (p,) = await _positions(db)
    assert p["initial_stop_price"] == pytest.approx(99.0)   # 101 - 2% = 98.98 on the tick; from 100: 98


async def test_a_flip_whose_old_side_closed_by_itself_closes_nothing(db, monkeypatch):
    eng = await _eng(db)
    await _run(eng, OPEN)

    async def gone(position, reason="manual", client=None):
        return "not_open"
    monkeypatch.setattr(eng, "execute_exit", gone)
    res, row = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "short", "size": {"qty": 1},
                                "stop": {"price": 105}})
    assert res["closed_position_ids"] == []
    assert row.status == "skipped"                                   # not "executed": nothing happened


async def test_a_flip_whose_new_side_filled_keeps_the_entrys_verdict(db, monkeypatch):
    eng = await _eng(db)
    await _run(eng, OPEN)

    async def entry(signal):                       # filled, but its stop could not be set
        await eng.signal_log.mark_outcome(signal["signal_id"], "stop_failed_position_open_unprotected")
        return {"success": False, "reason": "stop_failed_position_open_unprotected", "position_id": 99}
    monkeypatch.setattr(eng, "execute_entry", entry)
    _, row = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "short", "size": {"qty": 1},
                              "stop": {"price": 105}})
    assert (row.status, row.reasons) == ("executed", ["stop_failed_position_open_unprotected"])


async def test_an_entry_that_could_not_record_its_verdict_is_failed_not_accepted(db, monkeypatch):
    eng = await _eng(db)

    async def entry(signal):
        return {"success": False, "reason": "db_write_failed_recovery_pending"}
    monkeypatch.setattr(eng, "execute_entry", entry)
    _, row = await _run(eng, OPEN)
    assert (row.status, row.reasons) == ("failed", ["db_write_failed_recovery_pending"])


async def test_a_signal_cut_off_at_shutdown_is_marked_failed(db, monkeypatch):
    import asyncio
    eng = await _eng(db)
    started = asyncio.Event()

    async def entry(signal):
        started.set()
        await asyncio.Event().wait()                                 # never finishes by itself
    monkeypatch.setattr(eng, "execute_entry", entry)
    sig = parse_signal({**ENV, "id": "cut", **OPEN})
    row, _ = await eng.signal_log.record(sig.source, sig.id, {})
    task = asyncio.create_task(EventExecutor(eng, eng.signal_log).execute(sig, row.id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await eng.signal_log.get(row.id)
    assert (row.status, row.reasons) == ("failed", ["interrupted"])


async def test_close_all_waits_for_entries_under_way(db):
    import asyncio
    eng = await _eng(db)
    under_way = asyncio.get_running_loop().create_future()
    eng._entries_in_flight.add(under_way)
    task = asyncio.create_task(_run(eng, {"action": "close_all"}))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), 0.3)             # still waiting for the entry
    eng._entries_in_flight.discard(under_way)
    under_way.set_result(None)
    res, _ = await task
    assert res["success"]


async def test_a_reduce_on_a_position_closing_meanwhile_is_skipped(db, monkeypatch):
    eng = await _eng(db)
    await _run(eng, OPEN)

    async def closing(position, partial_pct, reason="reduce"):
        return {"success": False, "error": f"position {position['id']} is not open"}
    monkeypatch.setattr(eng, "execute_partial_exit", closing)
    res, row = await _run(eng, {"action": "reduce", "symbol": "BTCUSDT", "size": {"close_pct": 50}})
    assert (res["reason"], row.status) == ("no_position", "skipped")


def test_results_are_stored_as_valid_json():
    from relay.signals.executor import _jsonable
    assert _jsonable({"a": float("nan"), "b": [float("inf"), 1.5], "c": object}) == \
        {"a": None, "b": [None, 1.5], "c": str(object)}


@pytest.mark.parametrize("attr, body, reason", [
    ("can_short", {"side": "short", "size": {"qty": 2}, "stop": {"price": 105}}, "short_unsupported"),
    ("can_stop", {"side": "short", "size": {"qty": 2}, "stop": {"price": 105}}, "stop_unsupported"),
])
async def test_a_flip_the_venue_would_refuse_is_refused_before_the_old_side_is_closed(db, attr, body, reason):
    fake = FakeBybit()
    eng = await _eng(db, fake, settings={**SETTINGS, "require_stop": False})
    await _run(eng, {**OPEN, "stop": None} if attr == "can_stop" else OPEN)
    setattr(fake, attr, False)
    res, row = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", **body})
    assert (res["reason"], row.status) == (reason, "skipped")
    (pos,) = await _positions(db)
    assert pos["status"] == "OPEN" and pos["side"] == "LONG"            # the long was not closed
    if attr == "can_stop":
        res, _ = await _run(eng, {"action": "flip", "symbol": "BTCUSDT", "side": "short", "size": {"qty": 2}})
        assert res["success"], res                                       # without a stop it flips
