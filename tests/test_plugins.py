"""Plugins: the templates pass their own contracts, the relay loads a plugin only when a file names
it (and never in place of a built-in), and the contract checks catch a broken plugin."""
import json
import uuid
from datetime import UTC, datetime
from importlib.metadata import EntryPoint
from typing import ClassVar

import httpx
import pytest

from relay import cli, plugins
from relay.config import settings
from relay.plugins._template_sink import JsonPostSink
from relay.plugins._template_source import SimpleAlertSource
from relay.plugins._template_venue import TemplateVenue, build
from relay.plugins.contract import check_sink, check_source, check_venue
from relay.signals.sources import SourceConfigError, load_sources, sign
from relay.sinks import PluginSink, SinkConfigError, load_sinks
from relay.venue.factory import VenueConfigError, build_ccxt_venue, client_problems
from tests.test_engine import _no_sleep  # noqa: F401  (autouse fixture)

ENTRY_POINTS = {
    "quanti_relay.sources": [("simple-alert", "relay.plugins._template_source:SimpleAlertSource"),
                             ("no-translate", "tests.test_plugins:_NoTranslate")],
    "quanti_relay.sinks": [("json-post", "relay.plugins._template_sink:JsonPostSink"),
                           ("leaky-post", "tests.test_plugins:_Leaky"),
                           ("twice", "relay.plugins._template_sink:JsonPostSink"),
                           ("twice", "tests.test_plugins:_Leaky"),
                           ("telegram", "relay.plugins._template_sink:JsonPostSink")],
    "quanti_relay.venues": [("templatevenue", "relay.plugins._template_venue:build"),
                            ("okx", "relay.plugins._template_venue:build"),
                            ("brokenx", "tests.test_plugins:_broken_build"),
                            ("badid", "tests.test_plugins:_bad_id_build")],
}


@pytest.fixture(autouse=True)
def _installed(monkeypatch):
    def entry_points(group):
        return [EntryPoint(name, value, group) for name, value in ENTRY_POINTS.get(group, [])]
    monkeypatch.setattr(plugins, "entry_points", entry_points)


def _alert(**kw):
    a = {"id": uuid.uuid4().hex, "deployment": "default", "time": datetime.now(UTC).isoformat(),
         "ticker": "BTCUSDT", "action": "buy", "usd": 500, "stop_pct": 2}
    a.update(kw)
    return a


# ------------------------------------------------------------------ the templates pass their contracts
def test_the_source_template_passes_its_contract():
    check_source(SimpleAlertSource(), samples=[_alert(), _alert(action="sell"), _alert(action="exit"),
                                               _alert(stop_pct=None)],
                 unreadable=[{}, _alert(action="hold"), _alert(ticker="")])


async def test_the_sink_template_passes_its_contract():
    await check_sink(JsonPostSink, {"url_env": "PAGER_URL", "token_env": "PAGER_TOKEN"},
                     {"PAGER_URL": "https://pager.example/hook", "PAGER_TOKEN": "t" * 30})


async def test_the_venue_template_passes_its_contract():
    venue = build("testnet", paper=False, environ={})
    steps = await check_venue(lambda *a, **k: venue, environment="testnet", environ={}, symbol="BTCUSDT",
                              notional=500.0, stop_pct=4.0, rebuild=lambda: venue)
    assert not venue.positions and {s.name for s in steps} >= {"open", "stop", "move", "restart", "close"}


def test_installed_lists_the_registered_names():
    assert plugins.installed("sink") == ["json-post", "leaky-post", "telegram", "twice", "twice"]
    with pytest.raises(plugins.PluginError, match="two installed packages register a sink plugin named 'twice'"):
        plugins.load("sink", "twice")
    with pytest.raises(plugins.PluginError, match="no source plugin named 'nope'"):
        plugins.load("source", "nope")


# ------------------------------------------------------------------ the contract checks catch broken plugins
class _Mutating(SimpleAlertSource):
    def translate(self, payload):
        payload["seen"] = True
        return super().translate(payload)


class _Invalid(SimpleAlertSource):
    def translate(self, payload):
        return {**super().translate(payload), "kind": "nonsense"}


class _Crashing(SimpleAlertSource):
    def translate(self, payload):
        payload["ticker"].upper()                                      # KeyError on a payload without it
        return super().translate(payload)


@pytest.mark.parametrize("plugin, said", [(_Mutating(), "changed the payload"), (_Invalid(), "invalid v1 signal"),
                                          (_Crashing(), "not ValueError")])
def test_the_source_contract_catches(plugin, said):
    with pytest.raises(AssertionError, match=said):
        check_source(plugin, samples=[_alert()], unreadable=[{"ticker": ""}, {}])


class _Leaky(JsonPostSink):
    def redact(self, text):
        return text


class _ServerErrorRefused(JsonPostSink):
    async def send(self, http, event):
        from relay.sinks import SendError
        r = await http.post(self._url, json=event)
        if r.status_code >= 300:
            raise SendError("x", refused=r.status_code >= 400)


@pytest.mark.parametrize("cls, said", [(_Leaky, "secret"), (_ServerErrorRefused, "500 answer")])
async def test_the_sink_contract_catches(cls, said):
    with pytest.raises(AssertionError, match=said):
        await check_sink(cls, {"url_env": "U", "token_env": "T"}, {"U": "https://x.example/h", "T": "t" * 30})


async def test_the_venue_contract_catches_a_missing_call_and_a_failing_step():
    with pytest.raises(AssertionError, match="lacks the call"):
        await check_venue(_broken_build, environment="testnet", environ={}, symbol="BTCUSDT", notional=500.0,
                          stop_pct=4.0)
    venue = build("testnet", paper=False, environ={})

    async def no_stop(symbol, stop_loss=None, **kw):
        return {"retCode": 0, "retMsg": "OK"}                          # says yes, sets nothing
    venue.set_trading_stop = no_stop
    with pytest.raises(AssertionError, match="FAIL  stop"):
        await check_venue(lambda *a, **k: venue, environment="testnet", environ={}, symbol="BTCUSDT",
                          notional=500.0, stop_pct=4.0, rebuild=lambda: venue)
    assert not venue.positions                                         # cleaned up anyway


def _broken_build(environment, *, paper, environ):
    v = TemplateVenue()
    v.set_trading_stop = None
    return v


def _bad_id_build(environment, *, paper, environ):
    v = TemplateVenue()
    v.venue_id = "Bad Id!"
    return v


# ------------------------------------------------------------------ sinks
def _sink_file(tmp_path, text):
    (tmp_path / "sinks").mkdir(exist_ok=True)
    (tmp_path / "sinks" / "pager.yaml").write_text(text)
    return tmp_path


def test_a_sink_file_names_a_plugin_and_the_relay_keeps_its_secrets_out_of_the_log(tmp_path):
    env = {"PAGER_URL": "https://pager.example/hook", "PAGER_TOKEN": "secret-token-" + "x" * 20}
    (sink,) = load_sinks(_sink_file(tmp_path, "type: json-post\nmin_severity: warning\nkinds: [position.]\n"
                                              "url_env: PAGER_URL\ntoken_env: PAGER_TOKEN\n"), env)
    assert isinstance(sink, PluginSink) and sink.type == "json-post" and sink.min_severity == "warning"
    assert sink.wants("position.opened", "critical") and not sink.wants("relay.started", "critical")
    assert "secret-token" not in sink.redact(f"failed with {env['PAGER_TOKEN']} at {env['PAGER_URL']}")


@pytest.mark.parametrize("text, env, said", [
    ("type: json-post\ntoken: abc\nurl_env: U\n", {"U": "https://x"}, "never go in a config file"),
    ("type: json-post\nurl_env: U\ntoken_env: T\n", {"U": "https://x"}, "token_env must name a set"),
    ("type: json-post\nurl_env: U\ntoken_env: T\ncolour: red\n", {"U": "https://x", "T": "t"}, "refused its settings"),
    ("type: nope\n", {}, "or an installed sink plugin"),
])
def test_a_plugin_sink_file_the_relay_cannot_use_stops_the_start(tmp_path, text, env, said):
    with pytest.raises(SinkConfigError, match=said):
        load_sinks(_sink_file(tmp_path, text), env)


def test_a_plugin_cannot_take_a_built_in_sinks_name(tmp_path):
    (sink,) = load_sinks(_sink_file(tmp_path, "type: telegram\nbot_token_env: B\nchat_id_env: C\n"),
                         {"B": "b" * 20, "C": "1"})
    assert not isinstance(sink, PluginSink) and sink.type == "telegram"


async def test_a_plugin_sink_receives_events_through_the_notifier(db, tmp_path):
    from relay.events import EventLog
    from relay.sinks import Notifier
    await db.create_all()
    (sink,) = load_sinks(_sink_file(tmp_path, "type: json-post\nurl_env: U\ntoken_env: T\n"),
                         {"U": "https://pager.example/hook", "T": "t" * 30})
    got = []

    def handler(request):
        got.append((json.loads(request.content), request.headers["authorization"]))
        return httpx.Response(200)
    events = EventLog(db, None)
    notifier = Notifier(events, [sink], http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await notifier.start_cursors()
    await events.record("relay.started", "info", "started")
    await notifier.deliver_once()
    await notifier.close()
    assert [(e["kind"], auth) for e, auth in got] == [("relay.started", "Bearer " + "t" * 30)]


# ------------------------------------------------------------------ sources
SECRET = "k" * 40


def _source_file(tmp_path, text):
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / "alerts.yaml").write_text(text)
    return tmp_path


def test_a_source_file_names_a_format(tmp_path):
    srcs = load_sources(_source_file(tmp_path, "auth: hmac\nsecret_env: S\ndeployments: [default]\n"
                                               "format: simple-alert\n"), {"S": SECRET})
    body = srcs["alerts"].translate({**_alert(), "passphrase": "p"})
    assert body["source"] == "alerts" and body["action"] == "open" and "passphrase" not in body
    with pytest.raises(SourceConfigError, match="no source plugin named 'nope'"):
        load_sources(_source_file(tmp_path, "auth: hmac\nsecret_env: S\ndeployments: [default]\nformat: nope\n"),
                     {"S": SECRET})


@pytest.fixture
async def relay(db, tmp_path, monkeypatch):
    from relay.api.app import create_app
    from relay.runtime import Runtime
    from tests.test_api import _cfg
    from tests.test_engine import FakeBybit
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "alerts.yaml").write_text("auth: hmac\nsecret_env: ALERTS_SECRET\ndeployments: [default]\n"
                                        "format: simple-alert\n")
    (folder / "mybot.yaml").write_text("auth: hmac\nsecret_env: ALERTS_SECRET\ndeployments: [default]\n")
    monkeypatch.setenv("ALERTS_SECRET", SECRET)
    rt = Runtime(_cfg(config_dir=str(tmp_path)), db=db, client=FakeBybit())
    app = create_app(rt)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as client):
        yield client, rt


async def _post(client, path, body):
    raw = json.dumps(body).encode()
    return await client.post(path, content=raw, headers={"Content-Type": "application/json",
                                                         "X-Relay-Signature": sign(SECRET, raw)})


async def test_a_sender_posts_its_own_format_to_its_source(relay):
    client, rt = relay
    r = await _post(client, "/v1/sources/alerts/signals", _alert(stop_pct=None, source="mybot"))
    assert r.status_code == 202, r.text
    row = await rt.signal_log.get(r.json()["signal"])
    assert row.source == "alerts" and row.payload["action"] == "open" and row.payload["source"] == "alerts"
    r = await _post(client, "/v1/sources/alerts/signals", _alert(action="hold"))
    assert r.status_code == 422 and "simple-alert format could not read it" in r.json()["reasons"][0]


async def test_the_path_names_the_source_whose_secret_is_checked(relay):
    client, _ = relay
    raw = json.dumps(_alert()).encode()
    r = await client.post("/v1/sources/alerts/signals", content=raw,
                          headers={"Content-Type": "application/json", "X-Relay-Signature": sign("w" * 40, raw)})
    assert r.status_code == 401
    v1 = {"version": "1", "id": uuid.uuid4().hex, "source": "alerts", "deployment": "default",
          "sent_at": datetime.now(UTC).isoformat(), "kind": "event", "action": "close", "symbol": "BTCUSDT"}
    r = await _post(client, "/v1/sources/mybot/signals", v1)                 # a v1 body naming another source
    assert r.status_code == 422 and "the path 'mybot'" in r.json()["detail"]
    r = await _post(client, "/v1/sources/mybot/signals", {**v1, "source": "mybot"})
    assert r.status_code == 202


# ------------------------------------------------------------------ venues
def test_a_venue_plugin_is_built_and_checked_like_a_connector():
    v = build_ccxt_venue("templatevenue", "testnet", paper=True, environ={})
    assert isinstance(v, TemplateVenue) and v.plugin_name == "templatevenue"
    refuse, warn = client_problems(v, {"require_stop": True})
    assert refuse == [] and any("venue plugin" in w and "verify-venue" in w for w in warn)
    v.can_stop = False
    refuse, _ = client_problems(v, {"require_stop": True})
    assert len(refuse) == 1 and "templatevenue cannot hold a stop" in refuse[0]


def test_a_plugin_cannot_take_a_ccxt_exchanges_name():
    from relay.venue.ccxt_venue import CcxtVenue
    assert isinstance(build_ccxt_venue("okx", "testnet", paper=True, environ={}), CcxtVenue)


@pytest.mark.parametrize("name, market, said", [
    ("brokenx", "perpetual", "lacks the call"), ("badid", "perpetual", "it must be 'badid'"),
    ("templatevenue", "spot", "leave venue.market empty"), ("nopex", "perpetual", "no venue plugin named 'nopex'"),
])
def test_a_venue_plugin_the_relay_cannot_use_is_refused(name, market, said):
    with pytest.raises(VenueConfigError, match=said):
        build_ccxt_venue(name, "testnet", paper=True, environ={}, market=market)


async def test_the_relay_runs_on_a_venue_plugin(db, tmp_path):
    from relay.runtime import Runtime
    from tests.test_venue_setup import _cfg, _deployment
    rt = Runtime(_cfg(config_dir=str(_deployment(tmp_path, "venue:\n  exchange: templatevenue\n  environment: testnet\n"))),
                 db=db)
    await rt.start()
    try:
        assert isinstance(rt.client, TemplateVenue) and rt.venue == ("templatevenue", "testnet")
        async with db.acquire() as c:
            (acc,) = await c.fetch("SELECT venue FROM accounts")
        assert acc["venue"] == "templatevenue"
        from relay.venue import venue_of
        assert venue_of(rt.client) == acc["venue"]                     # every account check matches it
    finally:
        await rt.stop()


def _deployment_file(tmp_path, monkeypatch, text):
    (tmp_path / "deployments").mkdir(exist_ok=True)
    (tmp_path / "deployments" / "main.yaml").write_text(text)
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    monkeypatch.setattr(settings, "dry_run", True)


def test_check_config_and_verify_venue_work_with_a_venue_plugin(tmp_path, monkeypatch, capsys):
    _deployment_file(tmp_path, monkeypatch, "venue:\n  exchange: templatevenue\n  environment: testnet\n")
    assert cli.main(["check-config"]) == 0
    assert "venue: templatevenue - a venue plugin (venue id templatevenue)" in capsys.readouterr().out
    shared = TemplateVenue()
    ENTRY_POINTS["quanti_relay.venues"].append(("sharedx", "tests.test_plugins:_shared_build"))
    globals()["_SHARED"] = shared
    try:
        _deployment_file(tmp_path, monkeypatch, "venue:\n  exchange: sharedx\n  environment: testnet\n")
        code = cli.main(["verify-venue", "--symbol", "BTCUSDT", "--notional", "500", "--stop-pct", "4", "--confirm"])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "PASS  restart" in out and "RESULT: all steps passed" in out
    finally:
        ENTRY_POINTS["quanti_relay.venues"].pop()


def _shared_build(environment, *, paper, environ):
    """A new client on the same in-memory exchange (as a restart would get)."""
    shared, v = globals()["_SHARED"], TemplateVenue()
    v.venue_id = "sharedx"
    v.prices, v.positions, v.orders, v.fills = shared.prices, shared.positions, shared.orders, shared.fills
    v._ids = shared._ids
    return v


@pytest.mark.parametrize("method, path", [("POST", "/v1/sources/alerts/signals/x"), ("POST", "/v1/sources/a/b/signals"),
                                          ("GET", "/v1/sources/alerts/signals")])
async def test_only_the_exact_source_signal_path_skips_the_api_key(relay, method, path):
    client, _ = relay
    r = await client.request(method, path, content=b"{}")
    assert r.status_code == 401 and r.json()["detail"] == "Invalid API key"



class _NoTranslate:
    name = "no-translate"


class _Counting(SimpleAlertSource):
    n = 0

    def translate(self, payload):
        type(self).n += 1
        return {**super().translate(payload), "meta": {"n": self.n}}


def test_the_source_contract_catches_two_answers_for_one_payload():
    with pytest.raises(AssertionError, match="two answers"):
        check_source(_Counting(), samples=[_alert()], unreadable=[])


def test_what_a_source_plugin_returns_is_held_to_the_relays_rules(tmp_path):
    from relay.signals.sources import Source

    class Spoofing:
        def translate(self, payload):
            return {**SimpleAlertSource().translate(payload), "source": "someone-else", "passphrase": "x"}

    class NotAnObject:
        def translate(self, payload):
            return ["open"]
    src = Source(name="alerts", auth="hmac", secret_env="S", deployments=("default",), format="f",
                 _translator=Spoofing())
    body = src.translate(_alert())
    assert body["source"] == "alerts" and "passphrase" not in body
    bad = Source(name="alerts", auth="hmac", secret_env="S", deployments=("default",), format="f",
                 _translator=NotAnObject())
    with pytest.raises(TypeError, match="not a signal object"):
        bad.translate(_alert())
    with pytest.raises(SourceConfigError, match="has no translate"):
        load_sources(_source_file(tmp_path, "auth: hmac\nsecret_env: S\ndeployments: [default]\n"
                                            "format: no-translate\n"), {"S": SECRET})


def test_the_relay_hides_a_plugin_sinks_secrets_even_when_the_plugin_does_not(tmp_path):
    env = {"U": "https://pager.example/secret-path", "T": "secret-token-" + "x" * 20}
    (sink,) = load_sinks(_sink_file(tmp_path, "type: leaky-post\nurl_env: U\ntoken_env: T\n"), env)
    text = sink.redact(f"failed with {env['T']} at {env['U']}")
    assert "secret-token" not in text and "secret-path" not in text


async def test_a_v1_body_without_a_source_is_taken_for_the_source_in_the_path(relay):
    client, rt = relay
    v1 = {"version": "1", "id": uuid.uuid4().hex, "deployment": "default", "sent_at": datetime.now(UTC).isoformat(),
          "kind": "event", "action": "close", "symbol": "BTCUSDT"}
    r = await _post(client, "/v1/sources/mybot/signals", v1)
    assert r.status_code == 202, r.text
    row = await rt.signal_log.get(r.json()["signal"])
    assert row.source == "mybot" and row.payload["source"] == "mybot"



# ------------------------------------------------------------------ review round
def _no_flags_build(environment, *, paper, environ):
    v = TemplateVenue()
    v.venue_id = "noflags"
    v.can_stop = None
    return v


def _bare_build(environment, *, paper, environ):
    """Says what it must (stops, shorts) and nothing more."""
    class Bare(TemplateVenue):
        venue_id = "bare"
    for flag in ("can_ladder", "can_trail", "can_set_leverage", "can_set_margin_mode", "gives_pnl"):
        setattr(Bare, flag, property(lambda self: None))
    return Bare()


def test_a_venue_plugin_must_say_whether_it_holds_stops_and_shorts():
    ENTRY_POINTS["quanti_relay.venues"].append(("noflags", "tests.test_plugins:_no_flags_build"))
    try:
        with pytest.raises(VenueConfigError, match="must say can_stop = True or False"):
            build_ccxt_venue("noflags", "testnet", paper=True, environ={})
    finally:
        ENTRY_POINTS["quanti_relay.venues"].pop()


def test_what_a_venue_plugin_does_not_offer_is_refused():
    ENTRY_POINTS["quanti_relay.venues"].append(("bare", "tests.test_plugins:_bare_build"))
    try:
        v = build_ccxt_venue("bare", "testnet", paper=True, environ={})
    finally:
        ENTRY_POINTS["quanti_relay.venues"].pop()
    refuse, warn = client_problems(v, {"trailing_distance_pct": 1, "take_profit_ladder": [{"pct": 2}], "leverage": 3,
                                       "isolated_margin": True, "daily_loss_limit_pct": 2})
    assert sorted(r.split(" is set")[0] for r in refuse) == ["isolated_margin", "leverage", "take_profit_ladder",
                                                              "trailing_distance_pct"]
    assert any("gives_pnl" in w for w in warn)
    full = build_ccxt_venue("templatevenue", "testnet", paper=True, environ={})
    refuse, warn = client_problems(full, {"take_profit_ladder": [{"pct": 2}], "leverage": 3, "isolated_margin": True,
                                          "daily_loss_limit_pct": 2})
    assert refuse == [] and not any("gives_pnl" in w for w in warn)


def test_a_venue_plugin_needs_no_ccxt(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_ccxt(name, *a, **kw):
        if name.startswith("ccxt"):
            raise ImportError("no ccxt here")
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_ccxt)
    assert isinstance(build_ccxt_venue("templatevenue", "testnet", paper=True, environ={}), TemplateVenue)
    with pytest.raises(VenueConfigError, match="needs the CCXT library"):
        build_ccxt_venue("binance", "testnet", paper=True, environ={})


@pytest.mark.parametrize("key", ["auth_token", "dsn", "endpoint", "webhook", "routing_key", "api_secret"])
def test_a_plugin_sink_file_holds_no_secret_under_any_name(tmp_path, key):
    with pytest.raises(SinkConfigError, match="never go in a config file"):
        load_sinks(_sink_file(tmp_path, f"type: json-post\n{key}: abc\n"), {})


class _Peeking(JsonPostSink):
    seen: ClassVar[dict] = {}

    @classmethod
    def from_config(cls, name, settings, environ):
        cls.seen = dict(environ)
        return super().from_config(name, settings, environ)


def test_a_plugin_sink_sees_only_the_variables_its_file_names_and_an_off_sink_is_not_loaded(tmp_path):
    ENTRY_POINTS["quanti_relay.sinks"].append(("peek", "tests.test_plugins:_Peeking"))
    try:
        env = {"U": "https://x.example/h", "T": "t" * 30, "RELAY_API_KEY": "k" * 40}
        load_sinks(_sink_file(tmp_path, "type: peek\nurl_env: U\ntoken_env: T\n"), env)
        assert _Peeking.seen == {"U": env["U"], "T": env["T"]}
        _Peeking.seen = {}
        assert load_sinks(_sink_file(tmp_path, "type: peek\nenabled: false\nurl_env: U\ntoken_env: T\n"), {}) == []
        assert _Peeking.seen == {}                                       # not even built
    finally:
        ENTRY_POINTS["quanti_relay.sinks"].pop()


async def test_a_plugin_sink_that_hangs_crashes_or_breaks_redact_is_contained(tmp_path, monkeypatch):
    import asyncio

    import relay.sinks as sinks_mod
    from relay.sinks import SendError
    (sink,) = load_sinks(_sink_file(tmp_path, "type: json-post\nurl_env: U\ntoken_env: T\n"),
                         {"U": "https://x.example/h", "T": "t" * 30})
    monkeypatch.setattr(sinks_mod, "PLUGIN_SEND_TIMEOUT_S", 0.01)

    async def hang(http, event):
        await asyncio.sleep(10)

    async def crash(http, event):
        raise KeyError("oops")
    for behaviour, said in ((hang, "did not finish"), (crash, "KeyError")):
        sink.plugin.send = behaviour
        with pytest.raises(SendError, match=said) as e:
            await sink.send(None, {})
        assert not e.value.refused
    sink.plugin.redact = lambda text: 1 / 0
    assert "t" * 30 not in sink.redact("token " + "t" * 30)            # the relay's own redaction still runs


async def test_a_plugin_output_that_is_not_plain_json_is_a_422_not_a_crash(relay, monkeypatch):
    client, rt = relay

    class WithDatetime(SimpleAlertSource):
        def translate(self, payload):
            return {**super().translate(payload), "sent_at": datetime.now(UTC)}
    object.__setattr__(rt.sources["alerts"], "_translator", WithDatetime())
    r = await _post(client, "/v1/sources/alerts/signals", _alert())
    assert r.status_code == 422 and "could not read it" in r.json()["reasons"][0]
