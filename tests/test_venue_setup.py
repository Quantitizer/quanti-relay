"""Choosing the venue: the deployment file's venue section, building a CCXT exchange from it, what
the exchange cannot do, a venue switch with positions open, and check-config."""
from datetime import UTC, datetime

import pytest

from relay import cli
from relay.config import settings
from relay.deployment_config import DeploymentConfigError, load_deployment
from relay.runtime import Runtime
from relay.venue import venue_of
from relay.venue.ccxt_venue import CcxtVenue
from relay.venue.factory import VenueConfigError, build_ccxt_venue, key_env_names, venue_problems
from relay.venue.profiles import PROFILES, Capabilities, Profile, profile_for
from tests.fake_ccxt import FakeExchange
from tests.test_api import _cfg
from tests.test_engine import FakeBybit, _no_sleep  # noqa: F401  (autouse fixture)


def _deployment(tmp_path, text):
    (tmp_path / "deployments").mkdir(exist_ok=True)
    (tmp_path / "deployments" / "main.yaml").write_text(text)
    return tmp_path


# ------------------------------------------------------------------ the venue section
@pytest.mark.parametrize("text, venue", [
    ("dry_run: true\n", ("bybit", None)),
    ("venue:\n", ("bybit", None)),
    ("venue:\n  exchange: okx\n  environment: testnet\n", ("okx", "testnet")),
    ("venue: {exchange: binance, environment: live}\n", ("binance", "live")),
])
def test_the_venue_section(tmp_path, text, venue):
    dep = load_deployment(_deployment(tmp_path, text))
    assert (dep.venue.exchange, dep.venue.environment) == venue


@pytest.mark.parametrize("text, match", [
    ("venue:\n  exchange: okx\n", "environment is required for okx"),
    ("venue:\n  exchange: bybit\n  environment: demo\n", "leave venue.environment empty"),
    ("venue:\n  exchange: OKX\n  environment: demo\n", "exchange"),
    ("venue:\n  exchange: okx\n  environment: paper\n", "environment"),
    ("venue:\n  exchange: okx\n  environment: demo\n  keys: abc\n", "keys"),
])
def test_venue_sections_that_stop_the_start(tmp_path, text, match):
    with pytest.raises(DeploymentConfigError, match=match):
        load_deployment(_deployment(tmp_path, text))


# ------------------------------------------------------------------ building the exchange
KEYS = {"RELAY_OKX_API_KEY": "k", "RELAY_OKX_API_SECRET": "s", "RELAY_OKX_API_PASSPHRASE": "p"}


def test_keys_are_named_after_the_exchange():
    assert key_env_names(PROFILES["okx"]) == {"apiKey": "RELAY_OKX_API_KEY", "secret": "RELAY_OKX_API_SECRET",
                                              "password": "RELAY_OKX_API_PASSPHRASE"}
    assert key_env_names(PROFILES["hyperliquid"]) == {"walletAddress": "RELAY_HYPERLIQUID_WALLET_ADDRESS",
                                                      "privateKey": "RELAY_HYPERLIQUID_PRIVATE_KEY"}
    assert key_env_names(profile_for("woo")) == {"apiKey": "RELAY_WOO_API_KEY", "secret": "RELAY_WOO_API_SECRET"}


def test_a_live_exchange_is_built_with_its_keys_from_the_environment():
    v = build_ccxt_venue("okx", "live", paper=False, environ=KEYS)
    assert isinstance(v, CcxtVenue) and venue_of(v) == "okx"
    assert (v.x.apiKey, v.x.secret, v.x.password) == ("k", "s", "p")
    assert v.x.enableRateLimit and v.x.options["defaultType"] == "swap"


def _off_real_host(x) -> bool:
    return bool(getattr(x, "isSandboxModeEnabled", False) or x.options.get("enableDemoTrading")
                or x.options.get("sandboxMode"))


@pytest.mark.parametrize("exchange, environment", [
    ("okx", "testnet"), ("binance", "demo"), ("binance", "testnet"), ("bitget", "demo"), ("gate", "testnet"),
    ("bingx", "testnet"), ("hyperliquid", "testnet"), ("bybit", "demo"),
])
def test_testnet_and_demo_switch_the_exchange_off_its_real_host(exchange, environment):
    assert _off_real_host(build_ccxt_venue(exchange, environment, paper=True, environ={}).x)
    assert not _off_real_host(build_ccxt_venue(exchange, "live", paper=True, environ={}).x)


def test_okx_testnet_is_its_demo_trading():
    assert build_ccxt_venue("okx", "testnet", paper=True, environ={}).x.headers == {"x-simulated-trading": "1"}


def test_paper_needs_no_keys_live_does():
    v = build_ccxt_venue("okx", "testnet", paper=True, environ={})
    assert not v.x.apiKey
    with pytest.raises(VenueConfigError, match="RELAY_OKX_API_KEY, RELAY_OKX_API_SECRET, RELAY_OKX_API_PASSPHRASE "
                                                "not set"):
        build_ccxt_venue("okx", "testnet", paper=False, environ={})
    with pytest.raises(VenueConfigError, match="RELAY_OKX_API_PASSPHRASE not set"):
        build_ccxt_venue("okx", "testnet", paper=False, environ={k: v for k, v in KEYS.items() if "PASS" not in k})


@pytest.mark.parametrize("exchange, environment, match", [
    ("nosuchexchange", "live", "not an exchange CCXT knows"),
    ("mexc", "testnet", "offers no testnet environment"),        # MEXC has no test environment
    ("okx", "demo", "offers no demo environment"),                # OKX's demo is CCXT's sandbox: testnet
])
def test_exchanges_and_environments_that_cannot_be_used(exchange, environment, match):
    with pytest.raises(VenueConfigError, match=match):
        build_ccxt_venue(exchange, environment, paper=True, environ={})


def test_every_top_ten_profile_is_a_ccxt_exchange_and_none_claims_to_be_verified():
    import ccxt.async_support as ca
    assert len(PROFILES) == 10
    for name, p in PROFILES.items():
        assert name in ca.exchanges, name
        assert not p.verified                     # verified only after a run on the exchange's test account
        for env in p.environments:
            assert env in ("demo", "testnet")


# ------------------------------------------------------------------ what the exchange cannot do
def test_settings_the_exchange_cannot_carry_out_stop_the_start():
    bare = Profile("x", "bare", capabilities=Capabilities(native_stop=False, reduce_only_limit=False))
    refuse, warn = venue_problems(bare, {}, {"trailing_distance_pct": 1.0, "take_profit_ladder": [{"pct": 2}],
                                             "isolated_margin": True, "leverage": 3, "daily_loss_limit_pct": 2})
    assert len(refuse) == 5
    assert any("stop order" in r for r in refuse) and any("trailing" in r for r in refuse)
    assert any("take_profit_ladder" in r for r in refuse) and any("isolated_margin" in r for r in refuse)
    assert any("leverage" in r for r in refuse)
    assert any("daily_loss_limit_pct" in w for w in warn) and any("community connector" in w for w in warn)
    able = Profile("x", "able", verified=True, capabilities=Capabilities(trailing_stop=True, closed_pnl=True))
    has = {"createTrailingPercentOrder": True, "setMarginMode": True, "setLeverage": True, "createTriggerOrder": True}
    assert venue_problems(able, has, {"trailing_distance_pct": 1.0, "take_profit_ladder": [{"pct": 2}],
                                      "isolated_margin": False, "leverage": 3, "daily_loss_limit_pct": 2}) == ([], [])
    assert venue_problems(able, {"createTriggerOrder": True}, {"isolated_margin": None, "leverage": None}) == ([], [])


@pytest.mark.parametrize("flag", ["createStopLossOrder", "createTriggerOrder", "createStopOrder",
                                  "createStopMarketOrder"])
def test_an_exchange_without_stop_orders_is_refused_unless_the_owner_turns_require_stop_off(flag):
    p = Profile("x", "x", verified=True)
    refuse, warn = venue_problems(p, {}, {"require_stop": True})       # CCXT: no stop orders there
    assert len(refuse) == 1 and "require_stop: false" in refuse[0] and warn == []
    refuse, warn = venue_problems(p, {}, {"require_stop": False})
    assert refuse == [] and len(warn) == 1 and "no stop on the exchange" in warn[0]
    assert venue_problems(p, {}, {})[0]                                 # require_stop is on unless set off
    assert venue_problems(p, {flag: True}, {"require_stop": True}) == ([], [])


# ------------------------------------------------------------------ the runtime
FAKEX = Profile("fakex", "a fake exchange", capabilities=Capabilities(trailing_stop=False))


def _rt(db, tmp_path, text="venue:\n  exchange: fakex\n  environment: testnet\n", x=None, **cfg):
    x = x or FakeExchange()
    return Runtime(_cfg(config_dir=str(_deployment(tmp_path, text)), **cfg), db=db, client=CcxtVenue(x, FAKEX)), x


async def test_the_relay_runs_on_a_ccxt_exchange(db, tmp_path):
    rt, x = _rt(db, tmp_path)
    x.apiKey, x.secret = "k", "s"                                            # an account to set up
    await rt.start()
    try:
        assert rt.venue == ("fakex", "testnet") and x.hedged is False          # one-way mode set
        (started,) = [e for e in await rt.events.recent() if e.kind == "relay.started"]
        assert "venue host fakex testnet" in started.message
        async with db.acquire() as c:
            (acc,) = await c.fetch("SELECT venue, testnet, demo FROM accounts")
        assert (acc["venue"], bool(acc["testnet"]), bool(acc["demo"])) == ("fakex", True, False)
        assert rt.private_ws is None                                           # Bybit's feed only
    finally:
        await rt.stop()


async def test_a_setting_the_exchange_cannot_carry_out_stops_the_relay(db, tmp_path):
    rt, _ = _rt(db, tmp_path, "venue:\n  exchange: fakex\n  environment: testnet\n"
                              "position_management:\n  trailing_distance_pct: 1.5\n")
    with pytest.raises(VenueConfigError, match="trailing"):
        await rt.start()
    await rt.stop()


async def _open_live_position(db, venue="bybit"):
    from relay.db.models import Account, Deployment, Position
    async with db.session() as s:
        acc = Account(name="default", venue=venue, testnet=True)
        s.add(acc)
        await s.flush()
        dep = Deployment(name="main", account_id=acc.id)
        s.add(dep)
        await s.flush()
        s.add(Position(deployment_id=dep.id, symbol="BTCUSDT", side="LONG", size=1, entry_price=100, status="OPEN",
                       dry_run=False, opened_at=datetime.now(UTC)))
        await s.commit()


async def test_switching_venue_with_live_positions_open_is_refused(db, tmp_path):
    await db.create_all()
    await _open_live_position(db)                                          # open on bybit testnet
    rt, _ = _rt(db, tmp_path)
    with pytest.raises(VenueConfigError, match=r"1 live position\(s\) are open on bybit \(testnet\).*fakex \(testnet\)"):
        await rt.start()
    await rt.stop()


async def test_switching_bybit_from_testnet_to_demo_with_live_positions_open_is_refused(db, tmp_path):
    await db.create_all()
    await _open_live_position(db)
    rt = Runtime(_cfg(config_dir=str(_deployment(tmp_path, "dry_run: true\n")), bybit_demo=True), db=db,
                 client=FakeBybit())
    with pytest.raises(VenueConfigError, match="bybit \\(demo\\)"):
        await rt.start()
    await rt.stop()


async def test_the_same_venue_or_only_paper_positions_start_fine(db, tmp_path):
    await db.create_all()
    await _open_live_position(db, venue="fakex")
    rt, _ = _rt(db, tmp_path)
    await rt.start()
    await rt.stop()


# ------------------------------------------------------------------ check-config
def _check(tmp_path, monkeypatch, capsys, text, env=None):
    monkeypatch.setattr(settings, "config_dir", str(_deployment(tmp_path, text)))
    monkeypatch.setattr(settings, "dry_run", True)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = cli.main(["check-config"])
    return code, capsys.readouterr().out


def test_check_config_for_a_ccxt_exchange(tmp_path, monkeypatch, capsys):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    code, out = _check(tmp_path, monkeypatch, capsys, "venue:\n  exchange: okx\n  environment: testnet\n")
    assert code == 0, out
    assert "venue: okx, environment: testnet" in out
    assert "OKX perpetual swaps - community connector (not yet verified)" in out
    assert "not set: paper only" in out and "RELAY_OKX_API_PASSPHRASE" in out
    code, out = _check(tmp_path, monkeypatch, capsys, "venue:\n  exchange: okx\n  environment: live\n")
    assert "environment: MAINNET (real money)" in out


def test_check_config_refuses_what_the_exchange_cannot_do(tmp_path, monkeypatch, capsys):
    code, out = _check(tmp_path, monkeypatch, capsys, "venue:\n  exchange: xt\n  environment: live\n")
    assert code != 0 and "venue: PROBLEM - xt cannot hold a stop order" in out       # CCXT: no stops on xt
    code, out = _check(tmp_path, monkeypatch, capsys, "require_stop: false\nvenue:\n  exchange: xt\n"
                                                      "  environment: live\n")
    assert code == 0 and "PROBLEM" not in out and "no stop on the exchange" in out
    code, out = _check(tmp_path, monkeypatch, capsys, "venue:\n  exchange: mexc\n  environment: live\n")
    assert code == 0 and "PROBLEM" not in out                                        # stops again since 2026-03
    code, out = _check(tmp_path, monkeypatch, capsys, "venue:\n  exchange: mexc\n  environment: testnet\n")
    assert code != 0 and "offers no testnet environment" in out


async def test_paper_positions_do_not_hold_a_venue_switch(db, tmp_path):
    from relay.db.models import Position
    await db.create_all()
    await _open_live_position(db)
    async with db.session() as s:                                      # the open one is paper after all
        (p,) = (await s.execute(__import__("sqlalchemy").select(Position))).scalars().all()
        p.dry_run = True
        await s.commit()
    rt, _ = _rt(db, tmp_path)
    await rt.start()
    await rt.stop()


async def test_bybits_live_feed_is_not_started_for_another_exchange(db, tmp_path):
    called = []
    rt, _ = _rt(db, tmp_path, private_ws_enabled=True)
    rt._private_ws_factory = lambda: called.append(1)
    await rt.start()
    await rt.stop()
    assert called == []


@pytest.mark.parametrize("value, venue", [("okx", "okx"), (None, "bybit"), ("x' OR '1'='1", "bybit"),
                                          ("OKX", "bybit"), (7, "bybit")])
def test_the_venue_id_is_only_ever_a_plain_id(value, venue):
    class C:
        venue_id = value
    assert venue_of(C()) == venue
    assert venue_of(object()) == "bybit"


async def _account_with_live_row(db, venue):
    from relay.db.models import Account, Deployment, Position
    async with db.session() as s:
        acc = Account(name=f"acc-{venue}", venue=venue)
        s.add(acc)
        await s.flush()
        dep = Deployment(name=f"dep-{venue}", account_id=acc.id)
        s.add(dep)
        await s.flush()
        p = Position(deployment_id=dep.id, symbol=f"{venue.upper()[:3]}USDT", side="LONG", size=1, entry_price=100,
                     status="OPEN", dry_run=False, opened_at=datetime.now(UTC))
        s.add(p)
        await s.commit()
        return p.id


async def test_the_engine_sees_only_its_own_venues_account(db):
    from relay.execution.engine import ExecutionEngine
    await db.create_all()
    on_bybit = await _account_with_live_row(db, "bybit")
    on_fakex = await _account_with_live_row(db, "fakex")
    for client, mine in ((CcxtVenue(FakeExchange(), FAKEX), on_fakex), (FakeBybit(), on_bybit)):
        eng = ExecutionEngine(db, client, {}, dry_run=False)
        sql, args = eng._account_scope_sql(1)
        async with db.acquire() as c:
            rows = await c.fetch("SELECT id FROM positions WHERE status = 'OPEN'" + sql, *args)
        assert [r["id"] for r in rows] == [mine]


async def test_the_reconciler_prices_only_its_own_venues_account(db):
    from sqlalchemy import select

    from relay.db.models import Position
    from relay.execution.pnl_reconciler import _default_account_clause
    await db.create_all()
    on_bybit = await _account_with_live_row(db, "bybit")
    on_fakex = await _account_with_live_row(db, "fakex")
    async with db.session() as s:
        for venue, mine in (("fakex", on_fakex), ("bybit", on_bybit)):
            ids = (await s.execute(select(Position.id).where(_default_account_clause(venue)))).scalars().all()
            assert ids == [mine]
