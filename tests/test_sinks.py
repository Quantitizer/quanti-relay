"""Notification sinks: config, filters, the three senders, and delivery with its own place per sink."""
import json

import httpx
import pytest

from relay.events import EventLog
from relay.signals.sources import verify_signature
from relay.sinks import (
    BACKOFF_MAX_S,
    GAP_WAIT_S,
    MAX_ATTEMPTS,
    Notifier,
    SendError,
    SinkConfigError,
    load_sinks,
)

TOKEN = "123456:SECRET-TOKEN-abc"
ENV = {"TG_TOKEN": TOKEN, "TG_CHAT": "-1001", "DC_URL": "https://discord.example/api/webhooks/1/xyz",
       "WH_URL": "https://hooks.example.com/relay", "WH_SECRET": "w" * 40}


def _write(tmp_path, name, text):
    (tmp_path / "sinks").mkdir(exist_ok=True)
    (tmp_path / "sinks" / name).write_text(text)
    return tmp_path


TG = "type: telegram\nbot_token_env: TG_TOKEN\nchat_id_env: TG_CHAT\n"
DC = "type: discord\nwebhook_url_env: DC_URL\nmin_severity: warning\n"
WH = "type: webhook\nurl_env: WH_URL\nsecret_env: WH_SECRET\nkinds: [position., relay.halted]\n"


def test_sinks_load_with_their_secrets_from_the_environment(tmp_path):
    for name, text in (("phone.yaml", TG), ("team.yaml", DC), ("ops.yaml", WH)):
        _write(tmp_path, name, text)
    _write(tmp_path, "off.yaml", TG + "enabled: false\n")
    sinks = {s.name: s for s in load_sinks(tmp_path, environ=ENV)}
    assert set(sinks) == {"phone", "team", "ops"}                      # disabled: not loaded
    assert (sinks["team"].type, sinks["team"].min_severity) == ("discord", "warning")
    assert TOKEN not in repr(sinks["phone"])


@pytest.mark.parametrize("text, env, match", [
    ("type: email\n", ENV, "type must be one of"),
    (TG + "bot_token: abc\n", ENV, "never go in a config file"),
    (TG + "chat_id: 5\n", ENV, "never go in a config file"),
    (TG, {}, "is not set"),
    (TG.replace("chat_id_env: TG_CHAT\n", ""), ENV, "chat_id_env must name"),
    (DC, {**ENV, "DC_URL": "http://discord.example/x"}, "https://"),
    (WH, {**ENV, "WH_URL": "http://hooks.example.com"}, "https://"),
    (TG + "min_severity: loud\n", ENV, "min_severity"),
    (TG + "kinds: position.\n", ENV, "kinds must list"),
    (TG + "url_env: WH_URL\n", ENV, "unknown setting"),
    (TG + "enabled: maybe\n", ENV, "enabled"),
    ("type: telegram\nbot_token_env: TG_TOKEN\nbot_token_env: X\n", ENV, "set twice"),
])
def test_a_sink_the_relay_cannot_use_stops_the_start(tmp_path, text, env, match):
    with pytest.raises(SinkConfigError, match=match):
        load_sinks(_write(tmp_path, "s.yaml", text), environ=env)


def test_filters_by_severity_and_kind(tmp_path):
    _write(tmp_path, "team.yaml", DC)
    _write(tmp_path, "ops.yaml", WH)
    sinks = {s.name: s for s in load_sinks(tmp_path, environ=ENV)}
    team, ops = sinks["team"], sinks["ops"]
    assert not team.wants("signal.executed", "info") and team.wants("signal.failed", "warning")
    assert team.wants("relay.halted", "critical")
    assert ops.wants("position.closed", "info") and ops.wants("relay.halted", "critical")
    assert not ops.wants("relay.started", "info") and not ops.wants("signal.failed", "warning")


class _Hub:
    """Stands in for Telegram / Discord / a webhook receiver."""

    def __init__(self, fail=0, status=200, fail_status=500, fail_headers=None):
        self.requests, self.fail, self.status = [], fail, status
        self.fail_status, self.fail_headers = fail_status, fail_headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            self.fail -= 1
            return httpx.Response(self.fail_status, text="down", headers=self.fail_headers)
        return httpx.Response(self.status, json={"ok": True})


EVENT = {"id": 7, "kind": "position.closed", "severity": "info", "message": "BTCUSDT long closed @ 110: PnL +50.00",
         "data": {"pnl": 50.0}, "position_id": 3, "created_at": "2026-11-03T08:00:00+00:00"}


async def test_each_sender_speaks_its_own_format(tmp_path):
    for name, text in (("phone.yaml", TG), ("team.yaml", DC), ("ops.yaml", WH)):
        _write(tmp_path, name, text)
    sinks = {s.name: s for s in load_sinks(tmp_path, environ=ENV)}
    hub = _Hub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(hub)) as http:
        for name in ("phone", "team", "ops"):
            await sinks[name].send(http, EVENT)
    tg, dc, wh = hub.requests
    assert str(tg.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = json.loads(tg.content)
    assert body["chat_id"] == "-1001" and "position.closed" in body["text"] and "PnL +50.00" in body["text"]
    assert str(dc.url) == ENV["DC_URL"] and "PnL +50.00" in json.loads(dc.content)["content"]
    assert str(wh.url) == ENV["WH_URL"] and json.loads(wh.content)["data"] == {"pnl": 50.0}
    assert verify_signature(ENV["WH_SECRET"], wh.headers["X-Relay-Signature"], wh.content) is None


@pytest.mark.parametrize("status, refused", [(400, True), (403, True), (404, True), (408, False),
                                            (429, False), (500, False), (503, False)])
async def test_a_refusal_by_the_receiver_is_a_failure(tmp_path, status, refused):
    (sink,) = load_sinks(_write(tmp_path, "team.yaml", DC), environ=ENV)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_Hub(status=status))) as http:
        with pytest.raises(SendError, match=str(status)) as e:
            await sink.send(http, EVENT)
    assert e.value.refused is refused


@pytest.mark.parametrize("answer, wait", [
    (httpx.Response(429, headers={"Retry-After": "7"}), 7.0),                         # the header
    (httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 12}}), 12.0),  # Telegram
    (httpx.Response(429, json={"retry_after": 1.5}), 1.5),                            # Discord
    (httpx.Response(429, text="slow down"), None),
    (httpx.Response(503, headers={"Retry-After": "7"}), None),                        # only for 429
])
async def test_a_busy_receiver_says_how_long_to_wait(tmp_path, answer, wait):
    (sink,) = load_sinks(_write(tmp_path, "team.yaml", DC), environ=ENV)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: answer)) as http:
        with pytest.raises(SendError) as e:
            await sink.send(http, EVENT)
    assert e.value.retry_after == wait


WH_ALL = WH.replace("kinds: [position., relay.halted]\n", "")


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _notifier(db, tmp_path, hub):
    _write(tmp_path, "ops.yaml", WH_ALL)
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(hub)))
    n.clock = _Clock()
    return log, n


async def test_a_new_sink_starts_after_the_events_that_exist(db, tmp_path):
    await db.create_all()
    hub = _Hub()
    log, n = await _notifier(db, tmp_path, hub)
    await log.record("relay.started", "info", "old news")
    await n.start_cursors()
    await log.record("signal.executed", "info", "new")
    assert await n.deliver_once() == {"ops": 1}
    assert [json.loads(r.content)["message"] for r in hub.requests] == ["new"]
    assert await n.deliver_once() == {"ops": 0}                       # never twice


async def test_a_failed_send_is_retried_after_a_growing_wait_then_delivered_in_order(db, tmp_path):
    await db.create_all()
    hub = _Hub(fail=2)
    log, n = await _notifier(db, tmp_path, hub)
    await n.start_cursors()
    await log.record("signal.failed", "warning", "first")
    await log.record("signal.executed", "info", "second")
    assert await n.deliver_once() == {"ops": 0}                       # fails: wait 1 s
    assert await n.deliver_once() == {"ops": 0} and len(hub.requests) == 1   # not tried while waiting
    n.clock.t += 1
    assert await n.deliver_once() == {"ops": 0} and len(hub.requests) == 2   # fails again: wait 2 s
    n.clock.t += 1.5
    assert await n.deliver_once() == {"ops": 0} and len(hub.requests) == 2
    n.clock.t += 0.5
    assert await n.deliver_once() == {"ops": 2}
    delivered = [json.loads(r.content)["message"] for r in hub.requests[2:]]
    assert delivered == ["first", "second"]


async def test_a_long_outage_loses_nothing_and_the_wait_is_capped(db, tmp_path):
    await db.create_all()
    hub = _Hub(fail=40)
    log, n = await _notifier(db, tmp_path, hub)
    await n.start_cursors()
    await log.record("relay.halted", "critical", "halt")
    waits = []
    while hub.fail:
        assert (await n.deliver_once())["ops"] == 0
        waits.append(n._not_before["ops"] - n.clock.t)
        n.clock.t = n._not_before["ops"]
    assert waits[:4] == [1, 2, 4, 8] and max(waits) == BACKOFF_MAX_S == waits[-1]
    assert await n.deliver_once() == {"ops": 1}
    assert json.loads(hub.requests[-1].content)["message"] == "halt"


async def test_after_a_success_the_next_outage_starts_with_a_short_wait(db, tmp_path):
    await db.create_all()
    hub = _Hub(fail=3)
    log, n = await _notifier(db, tmp_path, hub)
    await n.start_cursors()
    await log.record("signal.executed", "info", "a")
    while hub.fail:
        await n.deliver_once()
        n.clock.t = n._not_before["ops"]
    assert await n.deliver_once() == {"ops": 1}
    hub.fail = 1
    await log.record("signal.executed", "info", "b")
    await n.deliver_once()
    assert n._not_before["ops"] - n.clock.t == 1


async def test_a_round_at_one_sink_stops_after_its_time_budget(db, tmp_path):
    from relay.sinks import ROUND_BUDGET_S
    await db.create_all()
    clock = _Clock()

    def slow(request):
        clock.t += ROUND_BUDGET_S * 0.6                # each send takes 60% of the budget
        return httpx.Response(200)
    log, n = await _notifier(db, tmp_path, slow)
    n.clock = clock
    await n.start_cursors()
    for i in range(5):
        await log.record("signal.executed", "info", f"e{i}")
    assert await n.deliver_once() == {"ops": 2}                       # the rest next round
    assert await n.deliver_once() == {"ops": 2}


async def test_waits_as_long_as_a_busy_receiver_asks(db, tmp_path):
    await db.create_all()
    hub = _Hub(fail=1, fail_status=429, fail_headers={"Retry-After": "30"})
    log, n = await _notifier(db, tmp_path, hub)
    await n.start_cursors()
    await log.record("signal.executed", "info", "x")
    assert (await n.deliver_once())["ops"] == 0
    n.clock.t += 29
    assert (await n.deliver_once())["ops"] == 0 and len(hub.requests) == 1
    n.clock.t += 1
    assert (await n.deliver_once())["ops"] == 1


async def test_an_event_the_receiver_keeps_refusing_is_skipped_after_the_last_attempt(db, tmp_path):
    await db.create_all()
    hub = _Hub(fail=MAX_ATTEMPTS, fail_status=400)
    log, n = await _notifier(db, tmp_path, hub)
    await n.start_cursors()
    await log.record("signal.failed", "warning", "poison")
    await log.record("signal.executed", "info", "next")
    for _ in range(MAX_ATTEMPTS - 1):
        assert (await n.deliver_once())["ops"] == 0                   # no waiting between refusals
    assert len(hub.requests) == MAX_ATTEMPTS - 1
    assert (await n.deliver_once())["ops"] == 1                       # the bad one skipped, the next sent
    assert json.loads(hub.requests[-1].content)["message"] == "next"


async def test_a_gap_in_the_ids_is_waited_for_until_it_is_old(db, tmp_path):
    from datetime import UTC, datetime, timedelta

    from relay.db.models import Event
    await db.create_all()
    hub = _Hub()
    log, n = await _notifier(db, tmp_path, hub)
    await n.start_cursors()
    await log.record("signal.executed", "info", "one")
    async with db.session() as s:                    # id 3 committed while id 2 is still on its way
        s.add(Event(id=3, kind="signal.executed", severity="info", message="three", created_at=datetime.now(UTC)))
        await s.commit()
    assert await n.deliver_once() == {"ops": 1}                       # one, then it waits for 2
    async with db.session() as s:
        s.add(Event(id=2, kind="signal.executed", severity="info", message="two", created_at=datetime.now(UTC)))
        await s.commit()
    assert await n.deliver_once() == {"ops": 2}
    assert [json.loads(r.content)["message"] for r in hub.requests] == ["one", "two", "three"]
    async with db.session() as s:                    # a gap that stays (a rolled-back insert)
        s.add(Event(id=5, kind="signal.executed", severity="info", message="five", created_at=datetime.now(UTC)))
        await s.commit()
    assert await n.deliver_once() == {"ops": 0}
    async with db.session() as s:
        (await s.get(Event, 5)).created_at = datetime.now(UTC) - timedelta(seconds=GAP_WAIT_S + 1)
        await s.commit()
    assert await n.deliver_once() == {"ops": 1}


async def test_a_slow_sink_does_not_hold_back_another(db, tmp_path):
    import asyncio
    await db.create_all()
    released = asyncio.Event()

    async def route(request):
        if "telegram" in str(request.url):
            await released.wait()                    # Telegram (served first) hangs
        return httpx.Response(200)
    for name, text in (("team.yaml", DC), ("phone.yaml", TG)):
        _write(tmp_path, name, text)
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(route)))
    await n.start_cursors()
    await log.record("signal.failed", "warning", "w")
    task = asyncio.create_task(n.deliver_once())
    for _ in range(200):
        if await log.cursor("sink:team") == await log.cursor("sink:phone") + 1:
            break
        await asyncio.sleep(0.01)
    assert await log.cursor("sink:team") == await log.cursor("sink:phone") + 1   # team done, phone stuck
    released.set()
    assert await task == {"team": 1, "phone": 1}


async def test_filtered_events_move_the_place_on_and_a_down_sink_holds_back_no_other(db, tmp_path):
    await db.create_all()

    def route(request):
        if "discord" in str(request.url):
            return httpx.Response(503, text="down")
        return httpx.Response(200)
    files = (("team.yaml", DC), ("phone.yaml", TG))
    for name, text in files:
        _write(tmp_path, name, text)
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(route)))
    await n.start_cursors()
    await log.record("signal.executed", "info", "info only")             # discord does not want it
    await log.record("signal.failed", "warning", "warning")
    out = await n.deliver_once()
    assert out == {"phone": 2, "team": 0}
    assert await log.cursor("sink:team") == await log.cursor("sink:phone") - 1   # stopped at the warning


async def test_a_failing_sinks_log_line_never_shows_its_token(db, tmp_path):
    from loguru import logger
    await db.create_all()

    def leaky(request):
        raise httpx.ConnectError(f"could not reach {request.url}")
    _write(tmp_path, "phone.yaml", TG)
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(leaky)))
    await n.start_cursors()
    await log.record("relay.halted", "critical", "halt")
    lines = []
    handle = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        await n.deliver_once()
    finally:
        logger.remove(handle)
    joined = "\n".join(lines)
    assert "not delivered" in joined and TOKEN not in joined and "***" in joined


async def test_a_successful_send_logs_no_token(db, tmp_path):
    """httpx logs every request URL at INFO; a Telegram URL carries the bot token."""
    import logging

    from loguru import logger

    from relay.logging_setup import route_stdlib_logging
    await db.create_all()
    _write(tmp_path, "phone.yaml", TG)
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(_Hub())))
    await n.start_cursors()
    await log.record("relay.halted", "critical", "halt")
    route_stdlib_logging()
    lines = []
    handle = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        assert await n.deliver_once() == {"phone": 1}
        logging.getLogger("relay.canary").info("canary")                   # the capture works
    finally:
        logger.remove(handle)
        logging.basicConfig(handlers=[], force=True)
    assert "canary" in "\n".join(lines) and TOKEN not in "\n".join(lines)


def test_a_logged_traceback_shows_no_variable_values(capsys):
    from loguru import logger

    from relay.logging_setup import route_stdlib_logging
    route_stdlib_logging()

    def fails():
        secret = "s3cr3t-in-a-local-variable"
        raise ValueError("boom" + secret[:0])      # diagnose would print secret's value here
    try:
        fails()
    except ValueError:
        logger.exception("it failed")
    err = capsys.readouterr().err
    assert "it failed" in err and "boom" in err and "s3cr3t-in-a-local-variable" not in err


async def test_shutdown_sends_what_it_reported_and_never_cuts_a_send(db, tmp_path, monkeypatch):
    import asyncio

    from relay.api.app import create_app
    from relay.runtime import Runtime
    from tests.test_api import _cfg
    from tests.test_engine import FakeBybit
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    import relay.sinks as sinks_mod
    _write(tmp_path, "ops.yaml", "type: webhook\nurl_env: WH_URL\n")
    sent, tried, started = [], [], asyncio.Event()

    async def slow(request):
        tried.append(json.loads(request.content)["kind"])
        started.set()
        await asyncio.sleep(0.3)                    # a send under way when the shutdown comes
        sent.append(json.loads(request.content)["kind"])
        return httpx.Response(200)
    real = httpx.AsyncClient
    monkeypatch.setattr(sinks_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(slow), **kw))
    await db.create_all()
    rt = Runtime(_cfg(config_dir=str(tmp_path), notify_interval_s=0), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 2)
        await rt.events.record("signal.executed", "info", "during shutdown")
    assert sent == ["relay.started", "signal.executed"]       # the send under way finished; the last one sent
    assert tried == sent                                      # nothing was cut and sent again
    assert await rt.events.cursor("sink:ops") == (await rt.events.recent(1))[0].id


async def test_the_relay_delivers_its_own_start(db, tmp_path, monkeypatch):
    from relay.api.app import create_app
    from relay.runtime import Runtime
    from tests.test_api import _cfg
    from tests.test_engine import FakeBybit
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    import relay.sinks as sinks_mod
    _write(tmp_path, "ops.yaml", "type: webhook\nurl_env: WH_URL\n")
    hub = _Hub()
    real = httpx.AsyncClient
    monkeypatch.setattr(sinks_mod.httpx, "AsyncClient",                 # no network: the hub answers
                        lambda **kw: real(transport=httpx.MockTransport(hub), **kw))
    await db.create_all()
    await EventLog(db).record("signal.executed", "info", "from before this relay ran")   # history
    rt = Runtime(_cfg(config_dir=str(tmp_path)), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        await rt.notifier.deliver_once()
        await rt.notifier.deliver_once()
    assert [json.loads(r.content)["kind"] for r in hub.requests] == ["relay.started"]   # once


async def test_the_runtime_loop_delivers(db, tmp_path):
    import asyncio

    from relay.api.app import create_app
    from relay.runtime import Runtime
    from tests.test_api import _cfg
    from tests.test_engine import FakeBybit
    rt = Runtime(_cfg(config_dir=str(tmp_path), notify_interval_s=1), db=db, client=FakeBybit())
    app = create_app(rt)
    ran = asyncio.Event()
    async with app.router.lifespan_context(app):
        async def round_():
            ran.set()
            return {}
        rt.notifier.deliver_once = round_
        await asyncio.wait_for(ran.wait(), 2)


async def test_two_rounds_at_once_deliver_each_event_once(db, tmp_path):
    import asyncio
    await db.create_all()
    seen = []

    async def slow(request):
        await asyncio.sleep(0)
        seen.append(json.loads(request.content)["message"])
        return httpx.Response(200)
    _write(tmp_path, "ops.yaml", WH_ALL)
    log = EventLog(db)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    await n.start_cursors()
    for i in range(3):
        await log.record("signal.executed", "info", f"e{i}")
    await asyncio.gather(n.deliver_once(), n.deliver_once())
    assert seen == ["e0", "e1", "e2"]


async def test_a_round_reports_new_positions(db, tmp_path):
    from tests.test_engine import FakeBybit, _engine, _sig
    hub = _Hub()
    eng, dep = await _engine(db, FakeBybit())
    _write(tmp_path, "ops.yaml", WH_ALL)
    log = EventLog(db, dep)
    n = Notifier(log, load_sinks(tmp_path, environ=ENV), httpx.AsyncClient(transport=httpx.MockTransport(hub)))
    await n.start_cursors()
    await n.deliver_once()                                             # the watcher starts here
    assert (await eng.execute_entry(_sig()))["success"]
    await n.deliver_once()
    assert [json.loads(r.content)["kind"] for r in hub.requests] == ["position.opened"]


def test_the_example_sink_files_load():
    from pathlib import Path
    examples = Path(__file__).resolve().parents[1] / "examples" / "config"
    env = {"RELAY_TELEGRAM_BOT_TOKEN": TOKEN, "RELAY_TELEGRAM_CHAT_ID": "1",
           "RELAY_DISCORD_WEBHOOK_URL": "https://discord.example/x", "RELAY_EVENTS_WEBHOOK_URL": "https://e.example",
           "RELAY_EVENTS_WEBHOOK_SECRET": "s" * 40}
    sinks = {s.name: s for s in load_sinks(examples, environ=env)}
    assert {n: (s.type, s.min_severity) for n, s in sinks.items()} == {
        "telegram": ("telegram", "info"), "discord": ("discord", "warning"), "webhook": ("webhook", "info")}
