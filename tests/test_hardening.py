"""The hardening round before the public release: what happens when the process dies mid-entry, the
database is down, a sender reuses an id, two relays share an account, or an answer is lost."""
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from relay.db.models import Deployment, Position
from tests.test_engine import FakeBybit, _engine, _no_sleep, _sig  # noqa: F401
from tests.test_sinks import ENV, EVENT, WH_ALL, _Clock, _Hub, _write

# ---- a fill nobody recorded is adopted with the stop it was sent with ----------------------------


class _Tagged(FakeBybit):
    """The venue's order history shows the relay's tagged opening order."""

    def __init__(self, tag, **kw):
        super().__init__(**kw)
        self.tag = tag

    async def get_order_history(self, **kw):
        if "order_link_id" in kw or "order_id" in kw:
            return await super().get_order_history(**kw)
        return [{"side": "Buy", "orderLinkId": self.tag, "reduceOnly": False, "orderStatus": "Filled"}]


async def test_a_live_entry_writes_its_stop_down_before_the_order_goes_out(db):
    fake = FakeBybit()
    seen = []
    real = fake.place_market_order

    async def place(**kw):
        async with db.acquire() as c:
            seen.extend(await c.fetch("SELECT order_link, stop_price, side FROM order_intents"))
        return await real(**kw)
    fake.place_market_order = place
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    assert r["success"], r
    assert len(seen) == 1 and seen[0]["stop_price"] == pytest.approx(95.0) and seen[0]["side"] == "LONG"
    assert seen[0]["order_link"] == dict(fake.calls)["place_market_order"]["order_link_id"]


async def test_a_paper_entry_writes_no_intent(db):
    eng, _ = await _engine(db, FakeBybit(), dry_run=True)
    assert (await eng.execute_entry(_sig()))["success"]
    async with db.acquire() as c:
        assert await c.fetch("SELECT * FROM order_intents") == []


async def test_no_intent_no_order(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)

    async def broken(*a, **kw):
        raise RuntimeError("disk full")
    eng._record_intent = broken
    r = await eng.execute_entry(_sig())
    assert not r["success"] and "place_market_order" not in fake.names()


@pytest.mark.parametrize("seen_as", ["full", "shortened"])
async def test_the_import_adopts_an_unrecorded_fill_with_its_intended_stop(db, seen_as):
    eng, dep = await _engine(db, FakeBybit(), dry_run=False)
    tag = f"rl-d{dep}-0123456789abcd"
    await eng._record_intent(tag, "BTCUSDT", "long", 95.0)
    shown = tag if seen_as == "full" else tag[:-6]               # a venue that keeps fewer digits
    fake = _Tagged(shown, positions=[{"symbol": "BTCUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
                                      "markPrice": "100", "stopLoss": ""}])
    eng.position_tracker.bybit = fake
    await eng.position_tracker.import_exchange_positions()
    async with db.acquire() as c:
        row = (await c.fetch("SELECT deployment_id, initial_stop_price, stop_price FROM positions"))[0]
    assert row["deployment_id"] == dep and row["initial_stop_price"] == pytest.approx(95.0)
    assert row["stop_price"] == pytest.approx(95.0)


async def test_an_intent_for_another_symbol_is_not_used(db):
    eng, dep = await _engine(db, FakeBybit(), dry_run=False)
    tag = f"rl-d{dep}-0123456789abcd"
    await eng._record_intent(tag, "ETHUSDT", "long", 95.0)
    fake = _Tagged(tag, positions=[{"symbol": "BTCUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
                                    "markPrice": "100", "stopLoss": ""}])
    eng.position_tracker.bybit = fake
    await eng.position_tracker.import_exchange_positions()
    async with db.acquire() as c:
        assert (await c.fetch("SELECT initial_stop_price FROM positions"))[0]["initial_stop_price"] is None


# ---- a position without a stop is said, and a live one is protected on paper too -------------------

_NAKED = {"symbol": "BTCUSDT", "side": "Buy", "size": "1", "avgPrice": "100", "markPrice": "101", "stopLoss": ""}


class _Events:
    def __init__(self):
        self.seen = []

    async def record(self, kind, severity, message, data=None, position_id=None):
        self.seen.append((kind, severity, data))


async def test_a_naked_position_is_a_critical_event_once_until_it_has_a_stop(db):
    fake = FakeBybit(positions=[_NAKED])
    eng, _ = await _engine(db, fake, dry_run=False)
    eng.events = _Events()
    await eng.audit_stops()
    await eng.audit_stops()                                       # still naked: said once
    assert eng.events.seen == [("position.naked", "critical", {"symbol": "BTCUSDT", "breached": False})]
    fake.positions = [{**_NAKED, "stopLoss": "95"}]
    await eng.audit_stops()
    fake.positions = [_NAKED]
    await eng.audit_stops()                                       # naked again: said again
    assert len(eng.events.seen) == 2


async def test_a_live_position_keeps_its_stop_while_the_deployment_is_on_paper(db):
    fake = FakeBybit(positions=[_NAKED])
    eng, dep = await _engine(db, fake, dry_run=True)              # put on paper to "pause" it
    async with db.session() as s:
        s.add(Position(deployment_id=dep, symbol="BTCUSDT", side="LONG", size=1, entry_price=100, status="OPEN",
                       dry_run=False, initial_stop_price=95.0, opened_at=datetime.now(UTC)))
        await s.commit()
    out = await eng.audit_stops()
    assert out["rearmed"] == 1 and "set_trading_stop" in fake.names()


# ---- the emergency stop holds across a restart ---------------------------------------------------

async def test_the_emergency_stop_holds_after_a_restart_until_the_owner_resumes(db):
    fake = FakeBybit()
    eng, dep = await _engine(db, fake, dry_run=True)
    await eng.start()
    await eng.close_all(reason="emergency_stop", stop_engine=True)
    assert not eng.is_running
    again, _ = await _restart(db, fake, dep)
    await again.start()
    try:
        assert again.halted_reason == "emergency_stop"
        assert not (await again.execute_entry(_sig()))["success"]
        events = _Events()
        again.events = events
        assert (await again.resume()) == {"resumed": True, "running": True}
        assert again.halted_reason is None and events.seen[0][0] == "relay.resumed"
        assert (await again.execute_entry(_sig()))["success"]
    finally:
        await again.stop()
    third, _ = await _restart(db, fake, dep)
    await third.start()
    try:
        assert third.halted_reason is None
    finally:
        await third.stop()


async def test_resume_starts_an_engine_the_emergency_stop_stopped(db):
    eng, _ = await _engine(db, FakeBybit(), dry_run=True)
    await eng.start()
    await eng.close_all(reason="emergency_stop", stop_engine=True)
    try:
        assert (await eng.resume())["running"] and eng.is_running
    finally:
        await eng.stop()


async def _restart(db, fake, dep):
    from relay.execution.engine import ExecutionEngine
    from tests.test_engine import SETTINGS
    eng = ExecutionEngine(db, fake, SETTINGS, dry_run=True, deployment_id=dep)
    await eng.initialize()
    return eng, dep


# ---- alerts while the database is down ------------------------------------------------------------

async def test_an_alert_the_database_refuses_goes_straight_to_the_sinks_and_is_stored_later(db, tmp_path):
    from relay.events import EventLog
    from relay.sinks import Notifier, load_sinks
    await db.create_all()
    _write(tmp_path, "ops.yaml", WH_ALL)
    hub = _Hub()
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(hub)))
    n.clock = _Clock()
    await n.start_cursors()
    real = db.session

    def down():
        raise ConnectionError("database down")
    db.session = down
    await log.record("relay.halted", "critical", "emergency_stop")
    await log.record("signal.executed", "info", "not kept")      # info is only logged
    assert [p["kind"] for p in log.pending] == ["relay.halted"]
    await n._send_pending()
    assert len(hub.requests) == 1 and b"relay.halted" in hub.requests[0].content
    await n._send_pending()
    assert len(hub.requests) == 1                                 # sent once per sink
    db.session = real
    await n.deliver_once()
    assert not log.pending and len(hub.requests) == 1             # stored, not sent twice
    (stored,) = await log.recent(kind="relay.halted")
    assert stored.data == {"_sent_to": ["ops"]}


async def test_the_delivery_round_sends_unstored_alerts_while_the_database_is_down(db, tmp_path, monkeypatch):
    from relay.events import EventLog
    from relay.sinks import Notifier, load_sinks
    await db.create_all()
    _write(tmp_path, "ops.yaml", WH_ALL)
    hub = _Hub()
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(hub)))
    n.clock = _Clock()
    await n.start_cursors()

    def down():
        raise ConnectionError("database down")
    monkeypatch.setattr(db, "session", down)
    await log.record("position.naked", "critical", "BTCUSDT without a stop")
    await n.deliver_once()
    assert len(hub.requests) == 1 and b"position.naked" in hub.requests[0].content


async def test_an_unstored_alert_a_sink_missed_is_tried_again(db, tmp_path):
    from relay.events import EventLog
    from relay.sinks import Notifier, load_sinks
    await db.create_all()
    _write(tmp_path, "ops.yaml", WH_ALL)
    hub = _Hub(fail=1)
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(hub)))
    n.clock = _Clock()
    log.pending.append({"kind": "position.naked", "severity": "critical", "message": "m", "data": None,
                        "position_id": None, "created_at": datetime.now(UTC), "sent_to": []})
    await n._send_pending()
    assert log.pending[0]["sent_to"] == []
    await n._send_pending()
    assert log.pending[0]["sent_to"] == ["ops"] and len(hub.requests) == 2


# ---- sinks that could stall -----------------------------------------------------------------------

async def test_a_redirecting_receiver_is_a_refusal_with_a_hint(tmp_path):
    from relay.sinks import SendError, load_sinks
    (sink,) = load_sinks(_write(tmp_path, "ops.yaml", WH_ALL), environ=ENV)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(301))) as http:
        with pytest.raises(SendError, match="final URL") as e:
            await sink.send(http, EVENT)
    assert e.value.refused


async def test_a_huge_retry_after_is_kept_to_the_longest_wait(db, tmp_path):
    from relay.sinks import BACKOFF_MAX_S
    from tests.test_sinks import _notifier
    await db.create_all()
    hub = _Hub(fail=1, fail_status=429, fail_headers={"Retry-After": "86400"})
    log, n = await _notifier(db, tmp_path, hub)
    await n.start_cursors()
    await log.record("signal.executed", "info", "x")
    await n.deliver_once()
    n.clock.t += BACKOFF_MAX_S
    assert (await n.deliver_once())["ops"] == 1


def test_a_switched_off_sink_needs_no_variables(tmp_path):
    from relay.sinks import load_sinks
    _write(tmp_path, "ops.yaml", WH_ALL + "enabled: false\n")
    assert load_sinks(tmp_path, environ={}) == []


# ---- the deployment's id goes into every order tag ------------------------------------------------

async def test_a_new_deployment_gets_a_random_id_so_two_relays_do_not_share_tags(db, tmp_path):
    from sqlalchemy import select

    from relay.runtime import Runtime
    from tests.test_api import _cfg
    ids = set()
    for name in ("one", "two", "three"):
        rt = Runtime(_cfg(deployment_name=name), db=db, client=FakeBybit())
        await db.create_all()
        rt.deployment = None
        ids.add(await rt._ensure_default_deployment())
    async with db.session() as s:
        assert set((await s.execute(select(Deployment.id))).scalars().all()) == ids
    assert len(ids) == 3 and all(1000 <= i < 1_000_000 for i in ids)


async def test_a_position_another_relay_opened_is_not_taken_for_its_own(db):
    eng, dep = await _engine(db, FakeBybit(), dry_run=False)
    fake = _Tagged(f"rl-d{dep + 777}-0123456789abcd", positions=[{**_NAKED}])
    eng.position_tracker.bybit = fake
    await eng.position_tracker.import_exchange_positions()
    async with db.acquire() as c:
        assert (await c.fetch("SELECT deployment_id FROM positions"))[0]["deployment_id"] is None


# ---- the server and the container -----------------------------------------------------------------

def test_docker_gives_the_relay_time_to_finish_an_entry():
    from relay.runtime import SIGNAL_DRAIN_S
    grace = yaml.safe_load(Path("compose.yaml").read_text())["services"]["relay"]["stop_grace_period"]
    assert grace.endswith("s") and int(grace[:-1]) > SIGNAL_DRAIN_S + 10


def test_the_server_takes_the_peer_address_not_a_forwarded_one(monkeypatch):
    import uvicorn

    from relay import cli
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setenv("RELAY_API_KEY", "t" * 40)
    cli.main(["serve"])
    assert seen.get("proxy_headers") is False
