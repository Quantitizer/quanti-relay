"""The hardening round, at the edges: a reused signal id, a slow plugin, the resume call, the status a
non-Bybit venue reports, lost answers on stop orders, and what the connectors claim."""
import asyncio

import ccxt
import pytest

from relay.venue.ccxt_venue import CcxtVenue
from relay.venue.profiles import PROFILES, Capabilities, Profile, profile_for
from tests.fake_ccxt import FakeExchange
from tests.test_api import KEY, api  # noqa: F401
from tests.test_signal_intake import _post, _settled, _signal, config_dir, relay  # noqa: F401

# ---- a reused signal id -------------------------------------------------------------------------


async def test_the_same_id_with_another_body_is_refused_and_said(relay):  # noqa: F811
    client, fake, rt = relay
    first = _signal()
    assert (await _post(client, first)).status_code == 202
    await _settled(rt)
    other = {**first, "action": "close"}
    for k in ("side", "size", "stop"):
        other.pop(k)
    r = await _post(client, other)
    assert r.status_code == 409 and r.json()["status"] == "conflict"
    (e,) = await rt.events.recent(kind="signal.conflict")
    assert first["id"] in e.message
    again = await _post(client, {**first, "sent_at": _signal()["sent_at"]})   # a retry, built again
    assert again.status_code == 202 and again.json()["status"] == "duplicate"
    assert len(await rt.signal_log.recent()) == 1                  # carried out once, the other not logged


async def test_an_order_id_reused_for_another_order_is_refused(api):  # noqa: F811
    client, fake, _ = api
    body = {"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95.0,
            "client_id": "c-1"}
    assert (await client.post("/v1/orders", json=body)).status_code == 200
    assert (await client.post("/v1/orders", json=body)).json()["status"] == "duplicate"
    r = await client.post("/v1/orders", json={**body, "symbol": "ETHUSDT"})
    assert r.status_code == 409


# ---- a slow source plugin ---------------------------------------------------------------------------

async def test_a_slow_plugin_is_cut_off_and_does_not_block_the_relay(relay, monkeypatch):  # noqa: F811
    import time

    import relay.api.app as app_mod
    client, _, rt = relay
    monkeypatch.setattr(app_mod, "TRANSLATE_TIMEOUT_S", 0.2)

    class Slow:
        def translate(self, payload):
            time.sleep(1)
            return payload
    src = rt.sources["mybot"]
    object.__setattr__(src, "format", "slow")
    object.__setattr__(src, "_translator", Slow())
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1
    t = asyncio.create_task(tick())
    try:
        r = await _post(client, _signal())
    finally:
        t.cancel()
    assert r.status_code == 422 and "took longer" in r.json()["reasons"][0]
    assert ticks >= 5                                               # the loop kept running meanwhile


# ---- resume and status ------------------------------------------------------------------------------

async def test_resume_needs_the_full_key_and_lifts_the_halt(api):  # noqa: F811
    client, _, rt = api
    await client.post("/v1/emergency-stop")
    assert (await client.get("/health")).status_code == 503
    r = await client.post("/v1/resume", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert (await client.post("/v1/resume")).json() == {"resumed": True, "running": True}
    assert (await client.get("/health")).status_code == 200


async def test_the_status_reports_the_venue_s_own_environment(api, monkeypatch):  # noqa: F811
    client, _, rt = api
    monkeypatch.setattr(type(rt), "venue", property(lambda self: ("okx", "demo")))
    s = (await client.get("/v1/status")).json()
    assert (s["venue"], s["environment"], s["demo"], s["testnet"]) == ("okx", "demo", True, False)


# ---- a lost answer on a stop order -------------------------------------------------------------------

class _LostAnswer(FakeExchange):
    """Places the order, then the answer never arrives."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.lose = set()

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        o = await super().create_order(symbol, type, side, amount, price, params)
        if "create_order" in self.lose and (params or {}).get("stopLossPrice"):
            self.lose.discard("create_order")
            raise ccxt.RequestTimeout("fake: timed out")
        return o

    async def create_trailing_percent_order(self, *a, **kw):
        o = await super().create_trailing_percent_order(*a, **kw)
        if "trailing" in self.lose:
            self.lose.discard("trailing")
            raise ccxt.RequestTimeout("fake: timed out")
        return o


_PROFILE = Profile("fakex", "fake", realized_pnl_field="pnl",
                   capabilities=Capabilities(trailing_stop=True, closed_pnl=True, funding_history=True))


async def test_a_stop_placed_whose_answer_was_lost_counts_as_placed_and_the_old_one_goes():
    x = _LostAnswer()
    v = CcxtVenue(x, _PROFILE)
    await v.place_market_order("BTCUSDT", "Buy", "0.05")
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.lose.add("create_order")
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=97.0))["retCode"] == 0
    resting = [o for o in x.orders.values() if o.get("stopLossPrice") and o["status"] == "open"]
    assert [o["stopLossPrice"] for o in resting] == [97.0]


async def test_a_stop_that_was_not_placed_is_still_a_failure():
    x = FakeExchange()
    v = CcxtVenue(x, _PROFILE)
    await v.place_market_order("BTCUSDT", "Buy", "0.05")
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.fail["create_order"] = ccxt.RequestTimeout("fake: timed out")
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=97.0))["retCode"] == -1


async def test_a_trailing_stop_whose_answer_was_lost_counts_as_placed():
    x = _LostAnswer()
    v = CcxtVenue(x, _PROFILE)
    await v.place_market_order("BTCUSDT", "Buy", "0.05")
    x.lose.add("trailing")
    assert (await v.set_trading_stop("BTCUSDT", trailing_stop=2.0))["retCode"] == 0


# ---- what the connectors claim ------------------------------------------------------------------------

@pytest.mark.parametrize("exchange", ["bitget", "kucoinfutures"])
def test_funding_is_not_claimed_where_ccxt_needs_a_symbol_for_it(exchange):
    assert PROFILES[exchange].capabilities.funding_history is False


def test_an_exchange_without_a_profile_asks_for_every_credential_ccxt_needs():
    assert profile_for("kucoin").credentials == ("apiKey", "secret", "password")
    assert profile_for("kraken").credentials == ("apiKey", "secret")
    assert profile_for("not-an-exchange").credentials == ("apiKey", "secret")


async def test_verify_venue_does_not_test_a_ladder_a_plugin_never_offered(monkeypatch):
    from relay.plugins import contract
    from relay.plugins._template_venue import TemplateVenue
    seen = {}

    async def fake_verify(venue, symbol, notional, stop_pct, rebuild, *, can_ladder):
        seen["ladder"] = can_ladder
        return []
    monkeypatch.setattr("relay.venue.verify.verify_venue", fake_verify)
    monkeypatch.setattr("relay.venue.verify.passed", lambda steps: True)

    class Silent:
        """A plugin that never mentions a ladder."""

        def __init__(self):
            self._inner = TemplateVenue()

        def __getattr__(self, name):
            if name == "can_ladder":
                raise AttributeError(name)
            return getattr(self._inner, name)
    await contract.check_venue(lambda env, *, paper, environ: Silent(), environment="testnet", environ={},
                               symbol="BTCUSDT", notional=1.0, stop_pct=1.0)
    assert seen["ladder"] is False


def test_a_ccxt_venue_s_ladder_follows_its_profile():
    from relay.venue.verify import ladder_offered
    for offered in (True, False):
        profile = Profile("fakex", "fake", capabilities=Capabilities(reduce_only_limit=offered))
        assert ladder_offered(CcxtVenue(FakeExchange(), profile)) is offered


# ---- what a source may send ---------------------------------------------------------------------

async def test_a_source_sends_only_the_actions_it_is_allowed(relay):  # noqa: F811
    client, _, rt = relay
    src = rt.sources["mybot"]
    object.__setattr__(src, "actions", ("open", "close"))
    assert (await _post(client, _signal())).status_code == 202
    r = await _post(client, {k: v for k, v in _signal(action="close_all").items() if k not in ("side", "size", "stop",
                                                                                                "symbol")})
    assert r.status_code == 403 and "may not send close_all" in r.json()["reasons"][0]
    book = {k: v for k, v in _signal(kind="target", weights={"BTCUSDT": 0.1}, stop_pct=5).items()
            if k not in ("action", "symbol", "side", "size", "stop")}
    r = await _post(client, book)
    assert r.status_code == 403 and "a target book" in r.json()["reasons"][0]
    await _settled(rt)


@pytest.mark.parametrize("text, match", [
    ("actions: [open, withdraw]\n", "actions must list"),
    ("actions: []\n", "actions must list"),
    ("name: api\n", "the relay's own"),
    ("name: console\n", "the relay's own"),
])
def test_a_source_file_with_bad_actions_or_a_reserved_name_stops_the_start(tmp_path, text, match):
    from relay.signals.sources import SourceConfigError, load_sources
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "s.yaml").write_text("auth: hmac\nsecret_env: S\ndeployments: [default]\n" + text)
    with pytest.raises(SourceConfigError, match=match):
        load_sources(tmp_path, environ={"S": "s" * 40})


def test_a_source_may_be_limited_to_target_books(tmp_path):
    from relay.signals.sources import load_sources
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "s.yaml").write_text("auth: hmac\nsecret_env: S\ndeployments: [default]\n"
                                                 "actions: [target]\n")
    src = load_sources(tmp_path, environ={"S": "s" * 40})["s"]
    assert src.may_send(type("T", (), {"kind": "target"})())
    assert not src.may_send(type("E", (), {"kind": "event", "action": "open"})())
