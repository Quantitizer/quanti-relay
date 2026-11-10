"""POST /v1/signals: signed signals from registered sources, through the real runtime and engine."""
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from relay.api.app import create_app
from relay.runtime import Runtime
from relay.signals.sources import SourceConfigError, sign
from tests.test_api import KEY, _cfg
from tests.test_engine import FakeBybit, _no_sleep  # noqa: F401  (autouse fixture)

SECRET = "k" * 40
OTHER = "o" * 40


def _now(delta_s=0):
    return (datetime.now(UTC) + timedelta(seconds=delta_s)).isoformat().replace("+00:00", "Z")


def _signal(**kw):
    s = {"version": "1", "id": uuid.uuid4().hex, "source": "mybot", "deployment": "default", "sent_at": _now(),
         "kind": "event", "action": "open", "symbol": "BTCUSDT", "side": "long",
         "size": {"notional_usd": 500}, "stop": {"price": 95}}
    s.update(kw)
    return s


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "mybot.yaml").write_text("auth: hmac\nsecret_env: MYBOT_SECRET\ndeployments: [default]\n")
    (folder / "elsewhere.yaml").write_text("auth: hmac\nsecret_env: OTHER_SECRET\ndeployments: [desk]\n")
    (folder / "wide.yaml").write_text("auth: hmac\nsecret_env: OTHER_SECRET\ndeployments: [default, desk]\n")
    (folder / "off.yaml").write_text("auth: hmac\nsecret_env: OTHER_SECRET\ndeployments: [default]\n"
                                     "enabled: false\n")
    monkeypatch.setenv("MYBOT_SECRET", SECRET)
    monkeypatch.setenv("OTHER_SECRET", OTHER)
    return tmp_path


@pytest.fixture
async def relay(db, config_dir):
    fake = FakeBybit()
    rt = Runtime(_cfg(config_dir=str(config_dir)), db=db, client=fake)
    app = create_app(rt)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as client):
        yield client, fake, rt


async def _post(client, body, secret=SECRET, raw=None, t=None, header=None):
    payload = raw if raw is not None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "X-Relay-Signature": header if header is not None else sign(secret, payload, t)}
    return await client.post("/v1/signals", content=payload, headers=headers)


async def _settled(rt):
    """Wait for the signals being carried out in the background."""
    while rt._signal_tasks:
        await asyncio.gather(*list(rt._signal_tasks))


async def _row(client, row_id):
    r = await client.get(f"/v1/signals/{row_id}", headers={"Authorization": f"Bearer {KEY}"})
    return r.json()


async def test_a_signed_signal_is_accepted_then_carried_out(relay):
    client, fake, rt = relay
    sig = _signal()
    r = await _post(client, sig)
    assert r.status_code == 202
    body = r.json()
    assert (body["id"], body["status"], body["reasons"]) == (sig["id"], "accepted", [])
    await _settled(rt)
    row = await _row(client, body["signal"])
    assert (row["status"], row["source"], row["signal_id"], row["symbol"]) == ("executed", "mybot", sig["id"],
                                                                              "BTC/USDT:USDT")
    assert row["result"]["success"] and row["result"]["dry_run"] is True    # the deployment is in dry-run
    assert "place_market_order" not in fake.names()


async def test_a_repeated_id_is_never_carried_out_twice(relay):
    client, _, rt = relay
    sig = _signal()
    first = (await _post(client, sig)).json()
    await _settled(rt)
    again = await _post(client, {**sig, "sent_at": _now()})          # re-signed, same id
    assert again.status_code == 202
    assert again.json() == {"id": sig["id"], "signal": first["signal"], "status": "duplicate",
                            "signal_status": "executed", "reasons": None}
    await _settled(rt)
    positions = (await client.get("/v1/positions", headers={"Authorization": f"Bearer {KEY}"})).json()
    assert len(positions) == 1


async def test_no_api_key_is_needed_but_reading_signals_still_needs_one(relay):
    client, _, rt = relay
    r = await _post(client, _signal())
    assert r.status_code == 202
    assert (await client.get(f"/v1/signals/{r.json()['signal']}")).status_code == 401
    assert (await client.post("/v1/orders", json={})).status_code == 401              # the rest is unchanged
    await _settled(rt)


@pytest.mark.parametrize("change, status, detail", [
    ({"source": "nobody"}, 401, "signature not accepted"),
    ({"source": 7}, 401, "signature not accepted"),
    ({"source": "off"}, 403, "disabled"),
])
async def test_sources_that_are_refused(relay, change, status, detail):
    client, _, _ = relay
    r = await _post(client, _signal(**change), secret=OTHER)
    assert r.status_code == status and detail in r.json()["detail"]


async def test_bad_signatures_are_refused_before_anything_is_read(relay):
    client, _, rt = relay
    sig = _signal()
    for kw in ({"secret": OTHER}, {"t": int(time.time()) - 120}, {"header": ""}, {"header": "garbage"}):
        r = await _post(client, sig, **kw)
        assert r.status_code == 401, kw
    signed = json.dumps(sig).encode()
    header = sign(SECRET, signed)
    tampered = signed.replace(b'"notional_usd": 500', b'"notional_usd": 5000')
    r = await client.post("/v1/signals", content=tampered, headers={"X-Relay-Signature": header})
    assert r.status_code == 401
    assert await rt.signal_log.get_by_source_id("mybot", sig["id"]) is None      # nothing logged


async def test_bodies_that_are_not_signals(relay):
    client, _, _ = relay
    assert (await _post(client, None, raw=b"not json")).status_code == 400
    assert (await _post(client, None, raw=b"[1, 2]")).status_code == 400
    big = json.dumps(_signal(padding="x" * 70_000)).encode()
    assert (await _post(client, None, raw=big)).status_code == 413


async def test_an_invalid_signal_is_rejected_with_reasons(relay):
    client, _, _ = relay
    r = await _post(client, _signal(size={"qty": True}, stop=None))
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected" and any("size.qty" in x for x in body["reasons"])


@pytest.mark.parametrize("change, status, reason", [
    ({"deployment": "desk"}, 403, "may not reach deployment desk"),
    ({"sent_at": _now(-120)}, 422, "stale"),
    ({"sent_at": _now(-10), "ttl_s": 5}, 422, "stale"),
    ({"sent_at": _now(3600), "ttl_s": 7200}, 422, "in the future"),
])
async def test_signals_rejected_after_reading_are_logged_under_their_id(relay, change, status, reason):
    client, fake, rt = relay
    sig = _signal(**change)
    r = await _post(client, sig)
    assert r.status_code == status and r.json()["status"] == "rejected"
    assert any(reason in x for x in r.json()["reasons"])
    row = await rt.signal_log.get_by_source_id("mybot", sig["id"])
    assert row.status == "rejected" and any(reason in x for x in row.reasons)
    await _settled(rt)
    assert "place_market_order" not in fake.names()


async def test_an_unknown_deployment_is_rejected(relay):
    client, _, _ = relay
    r = await _post(client, _signal(source="wide", deployment="desk"), secret=OTHER)
    assert r.status_code == 422 and r.json()["reasons"] == ["unknown deployment desk"]


async def test_a_refused_signal_is_recorded_as_skipped(relay):
    client, _, rt = relay
    body = {k: v for k, v in _signal().items() if k not in ("side", "size", "stop")} | {"action": "close"}
    r = await _post(client, body)
    assert r.status_code == 202
    await _settled(rt)
    row = await _row(client, r.json()["signal"])
    assert (row["status"], row["reasons"]) == ("skipped", ["no_position"])


def test_a_bad_source_file_stops_the_start(tmp_path, monkeypatch):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "bad.yaml").write_text("auth: hmac\nsecret_env: NOPE\ndeployments: [default]\n")
    monkeypatch.delenv("NOPE", raising=False)
    rt = Runtime(_cfg(config_dir=str(tmp_path)), db=None, client=FakeBybit())
    with pytest.raises(SourceConfigError, match="NOPE"):
        asyncio.run(rt.start())


async def test_shutdown_waits_for_a_signal_being_carried_out(db, config_dir):
    rt = Runtime(_cfg(config_dir=str(config_dir)), db=db, client=FakeBybit())
    app = create_app(rt)
    order = []

    async def slow():
        for _ in range(50):                 # still running when the shutdown begins
            await asyncio.sleep(0)
        order.append("signal done")
    async with app.router.lifespan_context(app):
        real_stop = rt.engine.stop

        async def engine_stop():
            order.append("engine stopped")
            await real_stop()
        rt.engine.stop = engine_stop
        rt.run_signal(slow())
    assert order == ["signal done", "engine stopped"]


async def test_an_unknown_source_and_a_bad_signature_look_the_same(relay):
    client, _, _ = relay
    unknown = await _post(client, _signal(source="nobody"))
    bad = await _post(client, _signal(), secret=OTHER)
    assert (unknown.status_code, unknown.json()) == (bad.status_code, bad.json())


@pytest.mark.parametrize("header", ["t=\u00b2,v1=ab", "t=" + "9" * 5000 + ",v1=ab", "t={now},v1=\u00e9\u00e9",
                                    "t={now},v1=zz"])
async def test_odd_signature_headers_are_a_401_not_a_crash(relay, header):
    client, _, _ = relay
    header = header.replace("{now}", str(int(time.time())))        # inside the window: the v1 is read
    r = await client.post("/v1/signals", content=json.dumps(_signal()).encode(),
                          headers={"X-Relay-Signature": header.encode("latin-1")})
    assert r.status_code == 401


async def test_a_declared_size_over_the_limit_is_refused_unread(relay):
    client, _, _ = relay

    async def never():
        raise AssertionError("the body was read")
        yield b""
    r = await client.post("/v1/signals", content=never(),
                          headers={"Content-Length": str(10**9), "X-Relay-Signature": "t=1,v1=ab"})
    assert r.status_code == 413


async def test_deeply_nested_json_is_a_400_not_a_crash(relay):
    client, _, _ = relay
    assert (await _post(client, None, raw=b"[" * 60_000)).status_code == 400


async def test_a_streamed_body_is_cut_off_at_the_limit(relay):
    client, _, _ = relay
    sent = []

    async def chunks():
        for _ in range(100):                                  # 100 x 8 KiB, no Content-Length
            sent.append(1)
            yield b"x" * 8192
    r = await client.post("/v1/signals", content=chunks(), headers={"X-Relay-Signature": "t=1,v1=ab"})
    assert r.status_code == 413


async def test_too_many_signals_at_once_are_turned_away_unlogged(relay):
    client, _, rt = relay
    from relay.api.app import MAX_SIGNALS_IN_FLIGHT
    blocker = asyncio.Event()
    for _ in range(MAX_SIGNALS_IN_FLIGHT):
        rt.run_signal(blocker.wait())
    sig = _signal()
    r = await _post(client, sig)
    assert r.status_code == 503 and "busy" in r.json()["reasons"][0]
    assert await rt.signal_log.get_by_source_id("mybot", sig["id"]) is None   # resend it: not a duplicate
    blocker.set()
    await _settled(rt)
    assert (await _post(client, {**sig, "sent_at": _now()})).json()["status"] == "accepted"
    await _settled(rt)


async def test_signals_cut_off_by_a_restart_are_marked_failed(db, config_dir):
    from relay.signals.log import SignalLog
    log = SignalLog(db)
    await db.create_all()
    row, _ = await log.record("mybot", "lost", {}, deployment="default", kind="event")
    other, _ = await log.record("mybot", "elsewhere", {}, deployment="desk", kind="event")
    rt = Runtime(_cfg(config_dir=str(config_dir)), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        pass
    assert (await log.get(row.id)).status == "failed" and "interrupted" in (await log.get(row.id)).reasons[0]
    assert (await log.get(other.id)).status == "accepted"         # another deployment's: not ours


async def test_a_target_book_is_accepted_then_carried_out(relay):
    client, fake, rt = relay
    sig = {k: v for k, v in _signal().items() if k not in ("action", "symbol", "side", "size", "stop")}
    sig.update(kind="target", weights={"BTCUSDT": 0.05, "ETH/USDT:USDT": -0.03}, stop_pct=2)
    r = await _post(client, sig)
    assert r.status_code == 202 and r.json()["status"] == "accepted"
    await _settled(rt)
    row = await _row(client, r.json()["signal"])
    assert row["status"] == "executed" and row["kind"] == "target" and row["result"]["dry_run"] is True
    assert sorted(lg["symbol"] for lg in row["result"]["legs"]) == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    positions = (await client.get("/v1/positions", headers={"Authorization": f"Bearer {KEY}"})).json()
    assert sorted((p["symbol"], p["side"]) for p in positions) == [("BTCUSDT", "LONG"), ("ETHUSDT", "SHORT")]
    assert "place_market_order" not in fake.names()
