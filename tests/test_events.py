"""Events: positions watched in the database, signal outcomes, stop failures, the halt."""
import uuid
from datetime import UTC, datetime, timedelta

from relay.events import EventLog
from relay.signals.contract import parse_signal
from relay.signals.executor import EventExecutor
from relay.signals.log import SignalLog
from tests.test_engine import FakeBybit, _engine, _no_sleep, _rows, _sig  # noqa: F401
from tests.test_signal_intake import _post, _signal, config_dir, relay  # noqa: F401  (fixtures)


async def _kinds(db):
    return [(r["kind"], r["severity"], r["position_id"]) for r in
            await _rows(db, "SELECT kind, severity, position_id FROM events ORDER BY id")]


async def test_positions_are_reported_once_when_they_open_and_close(db):
    fake = FakeBybit()
    eng, dep = await _engine(db, fake)
    log = EventLog(db, dep)
    assert await log.watch_positions() == 0                            # first run: from now on
    pid = (await eng.execute_entry(_sig()))["position_id"]
    assert await log.watch_positions() == 1
    fake.price = 110.0
    await eng.execute_exit({"id": pid}, reason="test")
    assert await log.watch_positions() == 1
    assert await log.watch_positions() == 0                            # each once
    assert await _kinds(db) == [("position.opened", "info", pid), ("position.closed", "info", pid)]
    opened, closed = await _rows(db, "SELECT message, data FROM events ORDER BY id")
    assert "BTCUSDT long 5 @ 100 (paper) opened, stop 95" == opened["message"]
    assert closed["message"] == "BTCUSDT long (paper) closed @ 110: PnL +50.00 (+10.00%)"


async def test_a_close_the_venue_made_is_reported_too_and_history_is_not(db):
    eng, dep = await _engine(db, FakeBybit(), dry_run=False)
    old = (await eng.execute_entry(_sig(symbol="ETHUSDT")))["position_id"]
    async with db.acquire() as c:                                    # closed before the watcher existed
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1 WHERE id = $2",
                        datetime.now(UTC) - timedelta(hours=2), old)
    log = EventLog(db, dep)
    await log.watch_positions()
    pid = (await eng.execute_entry(_sig()))["position_id"]
    async with db.acquire() as c:                                    # the stop fired on the venue: the sync
        await c.execute("UPDATE positions SET status = 'CLOSED', closed_at = $1, pnl = NULL WHERE id = $2",
                        datetime.now(UTC) + timedelta(seconds=1), pid)
    await log.watch_positions()
    kinds = await _kinds(db)
    assert ("position.closed", "info", pid) in kinds and not any(k[2] == old for k in kinds)
    (msg,) = await _rows(db, "SELECT message FROM events WHERE kind = 'position.closed'")
    assert "PnL pending" in msg["message"]


async def test_signal_outcomes_are_events(db):
    eng, dep = await _engine(db, FakeBybit())
    eng.signal_log = SignalLog(db)
    log = EventLog(db, dep)
    ex = EventExecutor(eng, eng.signal_log, log)
    env = {"version": "1", "source": "bot", "deployment": "default", "sent_at": "2026-11-03T08:00:00Z",
           "kind": "event"}
    for body in ({"action": "open", "symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500},
                  "stop": {"price": 95}},
                 {"action": "close", "symbol": "ETHUSDT"}):
        sig = parse_signal({**env, "id": uuid.uuid4().hex, **body})
        row, _ = await eng.signal_log.record("bot", sig.id, body)
        await ex.execute(sig, row.id)
    rows = await _rows(db, "SELECT kind, severity, message FROM events ORDER BY id")
    assert [(r["kind"], r["severity"]) for r in rows] == [("signal.executed", "info"), ("signal.skipped", "info")]
    assert rows[1]["message"] == "bot: close ETH/USDT:USDT skipped - no_position"


async def test_a_stop_that_cannot_be_set_is_a_critical_or_warning_event(db):
    fake = FakeBybit(stop_ok=False)
    eng, dep = await _engine(db, fake, dry_run=False)
    eng.events = EventLog(db, dep)
    r = await eng.execute_entry(_sig())
    assert not r["success"]
    ((kind, severity, pid),) = [k for k in await _kinds(db) if k[0] == "position.stop_failed"]
    assert pid == r["position_id"]
    assert severity == ("critical" if r["reason"] in ("stop_failed_close_unconfirmed",
                                                      "stop_failed_position_open_unprotected") else "warning")


async def test_the_emergency_stop_is_a_critical_event(db):
    eng, dep = await _engine(db, FakeBybit())
    eng.events = EventLog(db, dep)
    await eng.close_all(reason="emergency_stop", stop_engine=True)
    assert ("relay.halted", "critical", None) in await _kinds(db)


async def test_the_relay_reports_its_start_and_rejected_signals(relay):  # noqa: F811
    client, _, rt = relay
    r = await _post(client, _signal(deployment="desk"))                 # not bound: rejected
    assert r.status_code == 403
    rows = await _rows(rt.db, "SELECT kind, message FROM events ORDER BY id")
    assert rows[0]["kind"] == "relay.started" and "paper (dry-run)" in rows[0]["message"]
    assert rows[1]["kind"] == "signal.rejected" and "may not reach deployment desk" in rows[1]["message"]


async def test_a_close_still_being_confirmed_is_not_reported_as_closed(db):
    eng, dep = await _engine(db, FakeBybit(), dry_run=False)
    log = EventLog(db, dep)
    await log.watch_positions()
    pid = (await eng.execute_entry(_sig()))["position_id"]
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING', closed_at = $1 WHERE id = $2",
                        datetime.now(UTC) + timedelta(seconds=1), pid)
    await log.watch_positions()
    assert [k[0] for k in await _kinds(db)] == ["position.opened"]


async def test_a_position_left_without_a_stop_is_critical(db, monkeypatch):
    fake = FakeBybit(stop_ok=False)
    eng, dep = await _engine(db, fake, dry_run=False)
    eng.events = EventLog(db, dep)

    async def close_fails(position, reason="manual", **kw):
        return {"success": False, "error": "venue down"}
    monkeypatch.setattr(eng, "execute_exit_fast", close_fails)
    r = await eng.execute_entry(_sig())
    assert r["reason"] == "stop_failed_position_open_unprotected", r
    assert ("position.stop_failed", "critical", r["position_id"]) in await _kinds(db)


async def test_a_failed_signal_is_a_warning(db, monkeypatch):
    eng, dep = await _engine(db, FakeBybit())
    eng.signal_log = SignalLog(db)
    pid = (await eng.execute_entry(_sig()))["position_id"]

    async def broken(position, reason="manual", client=None):
        raise Exception("Failed to close position BTCUSDT: venue down")
    monkeypatch.setattr(eng, "execute_exit", broken)
    sig = parse_signal({"version": "1", "id": "f1", "source": "bot", "deployment": "default",
                        "sent_at": "2026-11-03T08:00:00Z", "kind": "event", "action": "close", "symbol": "BTCUSDT"})
    row, _ = await eng.signal_log.record("bot", sig.id, {})
    await EventExecutor(eng, eng.signal_log, EventLog(db, dep)).execute(sig, row.id)
    assert ("signal.failed", "warning", None) in await _kinds(db) and pid
