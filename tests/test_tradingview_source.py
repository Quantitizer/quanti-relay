"""Passphrase sources (TradingView): the address allowlist, the passphrase, and the intake path."""
import json
import uuid

import httpx
import pytest

from relay.api.app import create_app
from relay.runtime import Runtime
from relay.signals.sources import SourceConfigError, load_sources
from tests.test_api import _cfg
from tests.test_engine import FakeBybit, _no_sleep  # noqa: F401  (autouse fixture)
from tests.test_signal_intake import _now, _settled

PASS = "p" * 40
TV = ("auth: passphrase\nsecret_env: TV_PASS\ndeployments: [default]\n"
      "allow_ips: [192.0.2.10, 198.51.100.0/24]\n")


def _write(tmp_path, text, name="tv.yaml"):
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / name).write_text(text)
    return tmp_path


def _tv(tmp_path, text=TV):
    return load_sources(_write(tmp_path, text), environ={"TV_PASS": PASS})["tv"]


def test_the_address_and_the_passphrase_must_both_match(tmp_path):
    tv = _tv(tmp_path)
    assert tv.verify_passphrase(PASS, "192.0.2.10") is None
    assert tv.verify_passphrase(PASS, "198.51.100.77") is None            # in the CIDR range
    assert "not on the source's allow_ips" in tv.verify_passphrase(PASS, "203.0.113.5")
    assert "does not match" in tv.verify_passphrase("q" * 40, "192.0.2.10")
    assert "does not match" in tv.verify_passphrase(None, "192.0.2.10")
    assert "does not match" in tv.verify_passphrase(12345, "192.0.2.10")
    assert "unreadable" in tv.verify_passphrase(PASS, "not-an-ip")
    assert "unreadable" in tv.verify_passphrase(PASS, None)
    assert PASS not in repr(tv)


@pytest.mark.parametrize("text, match", [
    ("auth: passphrase\nsecret_env: TV_PASS\ndeployments: [default]\n", "needs allow_ips"),
    (TV.replace("[192.0.2.10, 198.51.100.0/24]", "[]"), "needs allow_ips"),
    (TV.replace("192.0.2.10", "192.0.2.300"), "allow_ips"),
    ("auth: hmac\nsecret_env: TV_PASS\ndeployments: [default]\nallow_ips: [192.0.2.10]\n", "belongs to a passphrase"),
    ("auth: hmac\nsecret_env: TV_PASS\ndeployments: [default]\nclient_ip_header: X-Forwarded-For\n", "client_ip_header"),
    (TV + "client_ip_header: ''\n", "client_ip_header"),
    (TV + "client_ip_header: X-Forwarded-For\n", "trusted_proxies must list"),
    (TV + "client_ip_header: X-Forwarded-For\ntrusted_proxies: []\n", "trusted_proxies must list"),
    (TV + "client_ip_header: X-Forwarded-For\ntrusted_proxies: [nope]\n", "trusted_proxies"),
    (TV + "trusted_proxies: [127.0.0.1]\n", "belongs with client_ip_header"),
    (TV.replace("passphrase", "password", 1), "auth must be hmac or passphrase"),
])
def test_passphrase_sources_that_stop_the_start(tmp_path, text, match):
    with pytest.raises(SourceConfigError, match=match):
        load_sources(_write(tmp_path, text), environ={"TV_PASS": PASS})


def test_a_short_passphrase_stops_the_start(tmp_path):
    with pytest.raises(SourceConfigError, match="at least 32"):
        load_sources(_write(tmp_path, TV), environ={"TV_PASS": "short"})


def _alert(**kw):
    a = {"version": "1", "id": f"tv-{uuid.uuid4().hex[:8]}", "source": "tv", "deployment": "default",
         "sent_at": _now(), "kind": "event", "action": "open", "symbol": "BYBIT:BTCUSDT.P", "side": "long",
         "size": {"notional_usd": 500}, "stop": {"pct": 2}, "passphrase": PASS}
    a.update(kw)
    return a


async def _relay(db, config_dir, client_ip):
    rt = Runtime(_cfg(config_dir=str(config_dir)), db=db, client=FakeBybit())
    app = create_app(rt)
    transport = httpx.ASGITransport(app=app, client=(client_ip, 4321))
    return rt, app, transport


async def _post(transport, body, headers=None):
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as c:
        return await c.post("/v1/signals", content=json.dumps(body).encode(),
                            headers={"Content-Type": "application/json", **(headers or {})})


async def test_a_tradingview_alert_from_its_address_is_carried_out_and_the_passphrase_never_stored(db, tmp_path,
                                                                                               monkeypatch):
    monkeypatch.setenv("TV_PASS", PASS)
    rt, app, transport = await _relay(db, _write(tmp_path, TV), "192.0.2.10")
    async with app.router.lifespan_context(app):
        alert = _alert()
        r = await _post(transport, alert)
        assert r.status_code == 202 and r.json()["status"] == "accepted", r.json()
        await _settled(rt)
        row = await rt.signal_log.get_by_source_id("tv", alert["id"])
        assert row.status == "executed" and "passphrase" not in row.payload
        assert PASS not in json.dumps(row.payload) and row.symbol == "BTC/USDT:USDT"


@pytest.mark.parametrize("client_ip, change", [
    ("203.0.113.5", {}),                                        # not TradingView's address
    ("192.0.2.10", {"passphrase": "x" * 40}),                   # wrong passphrase
    ("192.0.2.10", {"passphrase": None}),                       # none
])
async def test_alerts_that_are_refused(db, tmp_path, monkeypatch, client_ip, change):
    monkeypatch.setenv("TV_PASS", PASS)
    rt, app, transport = await _relay(db, _write(tmp_path, TV), client_ip)
    async with app.router.lifespan_context(app):
        alert = {k: v for k, v in _alert(**change).items() if v is not None}
        r = await _post(transport, alert)
        assert r.status_code == 401
        assert await rt.signal_log.get_by_source_id("tv", alert["id"]) is None


PROXIED = TV + "client_ip_header: X-Forwarded-For\ntrusted_proxies: [10.0.0.2, 10.0.1.0/24]\n"


@pytest.mark.parametrize("peer, header, address", [
    ("10.0.0.2", "192.0.2.10", "192.0.2.10"),
    ("10.0.0.2", "203.0.113.5, 192.0.2.10", "192.0.2.10"),          # the proxy appended the real one
    ("10.0.0.2", "192.0.2.10, 203.0.113.5", "203.0.113.5"),         # a forged left entry is ignored
    ("10.0.0.2", "192.0.2.10, 10.0.1.7", "192.0.2.10"),             # through two of our proxies
    ("10.0.0.2", "10.0.1.7", None),                                 # only proxies: no client
    ("10.0.0.2", None, None),
    ("203.0.113.5", "192.0.2.10", None),                            # not through the proxy at all
    (None, "192.0.2.10", None),
])
def test_behind_a_proxy_the_client_is_the_right_most_untrusted_entry(tmp_path, peer, header, address):
    tv = load_sources(_write(tmp_path, PROXIED), environ={"TV_PASS": PASS})["tv"]
    assert tv.client_address(peer, header) == address


def test_without_a_proxy_the_header_is_ignored(tmp_path):
    assert _tv(tmp_path).client_address("203.0.113.5", "192.0.2.10") == "203.0.113.5"


async def test_behind_a_proxy_the_named_header_gives_the_address(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_PASS", PASS)
    rt, app, transport = await _relay(db, _write(tmp_path, PROXIED), "10.0.0.2")       # the proxy
    async with app.router.lifespan_context(app):
        ok = await _post(transport, _alert(), {"X-Forwarded-For": "203.0.113.5, 192.0.2.10"})
        assert ok.status_code == 202, ok.json()
        forged = await _post(transport, _alert(), {"X-Forwarded-For": "192.0.2.10, 203.0.113.5"})
        assert forged.status_code == 401
        assert (await _post(transport, _alert())).status_code == 401              # no header: no address
        await _settled(rt)


async def test_behind_the_relays_own_proxy_a_source_needs_no_proxy_settings(db, tmp_path, monkeypatch):
    """deploy/install.sh's TLS proxy: RELAY_TRUSTED_PROXIES is enough for a TradingView source."""
    monkeypatch.setenv("TV_PASS", PASS)
    rt = Runtime(_cfg(config_dir=str(_write(tmp_path, TV)), trusted_proxies="172.30.77.0/24"), db=db,
                 client=FakeBybit())
    app = create_app(rt)
    proxy = httpx.ASGITransport(app=app, client=("172.30.77.3", 4321))
    outside = httpx.ASGITransport(app=app, client=("203.0.113.5", 4321))
    async with app.router.lifespan_context(app):
        ok = await _post(proxy, _alert(), {"X-Forwarded-For": "203.0.113.9, 192.0.2.10"})
        assert ok.status_code == 202, ok.json()
        assert (await _post(proxy, _alert(), {"X-Forwarded-For": "192.0.2.10, 203.0.113.5"})).status_code == 401
        assert (await _post(proxy, _alert())).status_code == 401                 # the proxy itself is not TradingView
        assert (await _post(outside, _alert(), {"X-Forwarded-For": "192.0.2.10"})).status_code == 401
        await _settled(rt)


async def test_a_header_from_outside_the_proxy_is_not_believed(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_PASS", PASS)
    rt, app, transport = await _relay(db, _write(tmp_path, PROXIED), "203.0.113.5")    # straight in
    async with app.router.lifespan_context(app):
        r = await _post(transport, _alert(), {"X-Forwarded-For": "192.0.2.10"})
        assert r.status_code == 401


async def test_a_passphrase_that_is_not_valid_text_is_refused_not_a_crash(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_PASS", PASS)
    tv = _tv(tmp_path)
    assert "does not match" in tv.verify_passphrase("\ud800", "192.0.2.10")
    rt, app, transport = await _relay(db, _write(tmp_path, TV), "192.0.2.10")
    async with app.router.lifespan_context(app):
        body = json.dumps(_alert(passphrase="\ud800")).encode()      # a lone surrogate: valid JSON
        assert b"\\ud800" in body
        async with httpx.AsyncClient(transport=transport, base_url="http://relay") as c:
            r = await c.post("/v1/signals", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 401


def test_refusals_are_logged_at_most_once_per_window_with_a_count():
    from loguru import logger

    from relay.api import app as app_mod
    app_mod._refusals.update(last=0.0, quiet=0)
    lines = []
    handle = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        t = 10_000.0
        assert app_mod._log_refusal("x", "bad", now=t)
        assert not app_mod._log_refusal("x", "bad", now=t + 1)
        assert not app_mod._log_refusal("x", "bad", now=t + app_mod.REFUSAL_LOG_EVERY_S - 0.1)
        assert app_mod._log_refusal("x", "bad", now=t + app_mod.REFUSAL_LOG_EVERY_S)
    finally:
        logger.remove(handle)
        app_mod._refusals.update(last=0.0, quiet=0)
    assert len(lines) == 2 and "2 more refused" in lines[1] and "more refused" not in lines[0]


async def test_an_hmac_source_does_not_accept_a_passphrase(db, tmp_path, monkeypatch):
    monkeypatch.setenv("TV_PASS", PASS)
    _write(tmp_path, "auth: hmac\nsecret_env: TV_PASS\ndeployments: [default]\n", name="bot.yaml")
    rt, app, transport = await _relay(db, tmp_path, "192.0.2.10")
    async with app.router.lifespan_context(app):
        r = await _post(transport, _alert(source="bot"))
        assert r.status_code == 401


def test_the_example_file_is_not_loaded_until_it_is_filled_in(tmp_path):
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "examples" / "config" / "sources" / "tradingview.yaml.example"
    text = example.read_text()
    with pytest.raises(SourceConfigError, match="needs allow_ips"):            # as shipped: refuses to start
        load_sources(_write(tmp_path, text, "tradingview.yaml"), environ={"RELAY_TRADINGVIEW_PASSPHRASE": PASS})
    filled = text.replace("allow_ips: []", "allow_ips: [192.0.2.10]")
    src = load_sources(_write(tmp_path, filled, "tradingview.yaml"),
                       environ={"RELAY_TRADINGVIEW_PASSPHRASE": PASS})["tradingview"]
    assert src.auth == "passphrase" and src.may_reach("main")
