"""Risk rails: the pure checks (relay.rails), their config, and how the engine applies them."""
from datetime import UTC, datetime, timedelta

import pytest

from relay.deployment_config import DeploymentConfigError, load_deployment
from relay.rails import (
    DAYS,
    notional_cap,
    outside_trading_hours,
    stop_past_bankruptcy,
    symbol_refusal,
)
from tests.test_engine import SETTINGS, FakeBybit, _engine, _no_sleep, _rows, _sig  # noqa: F401

BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"


def test_symbol_lists():
    assert symbol_refusal(None, BTC) is None
    assert symbol_refusal({"symbols": {"allow": [BTC], "deny": []}}, BTC) is None
    assert "not on the allow list" in symbol_refusal({"symbols": {"allow": [BTC], "deny": []}}, ETH)
    assert "deny list" in symbol_refusal({"symbols": {"allow": [], "deny": [ETH]}}, ETH)
    assert "deny list" in symbol_refusal({"symbols": {"allow": [ETH], "deny": [ETH]}}, ETH)   # deny wins


def _at(day, hhmm, tz=UTC):
    # 2026-11-02 is a Monday
    d = datetime(2026, 11, 2, int(hhmm[:2]), int(hhmm[3:]), tzinfo=tz) + timedelta(days=DAYS.index(day))
    return d


WEEKDAYS = {"trading_hours": {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "08:00", "end": "20:00",
                              "timezone": "UTC"}}
NIGHT = {"trading_hours": {"days": ["fri"], "start": "22:00", "end": "06:00", "timezone": "UTC"}}


@pytest.mark.parametrize("rails, when, inside", [
    (None, _at("sun", "03:00"), True),
    (WEEKDAYS, _at("mon", "08:00"), True),                 # start is inside
    (WEEKDAYS, _at("mon", "19:59"), True),
    (WEEKDAYS, _at("mon", "20:00"), False),                # end is outside
    (WEEKDAYS, _at("mon", "07:59"), False),
    (WEEKDAYS, _at("sat", "12:00"), False),
    (NIGHT, _at("fri", "23:00"), True),
    (NIGHT, _at("sat", "05:59"), True),                    # the night that started on Friday
    (NIGHT, _at("sat", "06:00"), False),
    (NIGHT, _at("sat", "23:00"), False),                   # Saturday's night is not a trading night
    (NIGHT, _at("fri", "05:00"), False),                   # Thursday's night is not one either
])
def test_trading_hours(rails, when, inside):
    assert (outside_trading_hours(rails, when) is None) is inside


def test_trading_hours_follow_their_timezone():
    berlin = {"trading_hours": {"days": list(DAYS), "start": "09:00", "end": "17:00", "timezone": "Europe/Berlin"}}
    assert outside_trading_hours(berlin, _at("mon", "08:30")) is None          # 09:30 in Berlin (UTC+1)
    assert "Europe/Berlin" in outside_trading_hours(berlin, _at("mon", "16:30"))   # 17:30 in Berlin


def test_notional_cap_is_the_tightest_rail_and_never_negative():
    assert notional_cap(None, 10_000, 0) is None
    assert notional_cap({"max_notional_usd": 900}, 10_000, 0) == (900, "max_notional_usd")
    assert notional_cap({"max_notional_usd": 900, "max_notional_pct": 5}, 10_000, 0) == (500, "max_notional_pct")
    assert notional_cap({"max_gross_leverage": 0.5}, 10_000, 3_000) == (2_000, "max_gross_leverage")
    assert notional_cap({"max_gross_leverage": 0.5}, 10_000, 7_000) == (0, "max_gross_leverage")


@pytest.mark.parametrize("direction, stop, refused", [("long", 90.0, True), ("long", 90.1, False),
                                                      ("short", 110.0, True), ("short", 109.9, False)])
def test_a_stop_past_the_bankruptcy_price(direction, stop, refused):
    assert (stop_past_bankruptcy(None, direction, 100.0, stop, 10, True) is not None) is refused


def test_the_bankruptcy_rail_needs_a_leverage_isolated_margin_and_can_be_switched_off():
    assert stop_past_bankruptcy(None, "long", 100.0, 50.0, None, True) is None
    assert stop_past_bankruptcy({"stop_before_bankruptcy": False}, "long", 100.0, 50.0, 10, True) is None
    assert stop_past_bankruptcy(None, "long", 100.0, None, 10, True) is None
    assert stop_past_bankruptcy(None, "long", 100.0, 50.0, 10, False) is None      # cross: the balance backs it
    assert stop_past_bankruptcy(None, "long", 100.0, 50.0, 10, None) is None       # mode unknown


def _deploy(tmp_path, text):
    (tmp_path / "deployments").mkdir(exist_ok=True)
    (tmp_path / "deployments" / "main.yaml").write_text(text)
    return load_deployment(tmp_path)


def test_rails_config_reads_any_symbol_spelling(tmp_path):
    dep = _deploy(tmp_path, "rails: {symbols: {allow: [BTCUSDT, 'BYBIT:ETHUSDT.P'], deny: [SOL/USDT:USDT]}}")
    assert dep.rails.symbols.allow == [BTC, ETH] and dep.rails.symbols.deny == ["SOL/USDT:USDT"]


@pytest.mark.parametrize("text, match", [
    ("rails: {symbols: {allow: [BTC/USDT]}}", "spot pair"),
    ("rails: {trading_hours: {start: '24:00', end: '06:00'}}", "pattern"),
    ("rails: {trading_hours: {start: '08:00', end: '08:00'}}", "must differ"),
    ("rails: {trading_hours: {start: '08:00', end: '09:00', timezone: Mars/Base}}", "unknown timezone"),
    ("rails: {trading_hours: {start: '08:00', end: '09:00', days: [monday]}}", "days"),
    ("rails: {max_gross_leverage: 0}", "max_gross_leverage"),
    ("rails: {on_limit: shrink}", "on_limit"),
])
def test_rails_the_relay_cannot_use_stop_the_start(tmp_path, text, match):
    with pytest.raises(DeploymentConfigError, match=match):
        _deploy(tmp_path, text)


def _rails(**kw):
    base = {"symbols": {"allow": [], "deny": []}, "max_notional_usd": None, "max_notional_pct": None,
            "max_gross_leverage": None, "on_limit": "clip", "trading_hours": None, "stop_before_bankruptcy": True}
    return {**SETTINGS, "rails": {**base, **kw}}


async def test_the_engine_refuses_symbols_off_its_lists(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings=_rails(symbols={"allow": [BTC], "deny": []}))
    r = await eng.execute_entry(_sig(symbol="ETHUSDT"))
    assert r["reason"].startswith("symbol_not_allowed") and "get_ticker" not in fake.names()
    assert (await eng.execute_entry(_sig()))["success"]


async def test_the_engine_refuses_entries_outside_trading_hours(db):
    today = DAYS[datetime.now(UTC).weekday()]
    closed_today = {"days": [d for d in DAYS if d != today], "start": "00:00", "end": "23:59", "timezone": "UTC"}
    eng, _ = await _engine(db, FakeBybit(), settings=_rails(trading_hours=closed_today))
    assert (await eng.execute_entry(_sig()))["reason"].startswith("outside_trading_hours")


async def test_an_order_over_a_notional_rail_is_clipped(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**_rails(max_notional_usd=200), "max_size_shortfall_pct": 1})
    r = await eng.execute_entry(_sig())                              # 500 USD at 100 = 5
    assert r["success"], r
    assert r["sizing"]["quantity"] == 2.0 and r["sizing"]["rails"] == "clipped to max_notional_usd (200.00)"
    assert r["sizing"]["shortfall_pct"] == 0                         # the venue took nothing off


async def test_an_order_over_a_notional_rail_is_refused_when_set_to_reject(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings=_rails(max_notional_pct=1, on_limit="reject"))   # 1% of 10000
    r = await eng.execute_entry(_sig())
    assert r["reason"].startswith("rail_max_notional_pct") and "place_market_order" not in fake.names()


async def test_gross_leverage_counts_the_open_book_and_a_full_book_refuses(db):
    eng, _ = await _engine(db, FakeBybit(), settings=_rails(max_gross_leverage=0.06))    # 600 of 10000
    assert (await eng.execute_entry(_sig()))["sizing"]["quantity"] == 5.0              # 500: fits
    r = await eng.execute_entry(_sig(symbol="ETHUSDT"))                                 # 100 left
    assert r["success"] and r["sizing"]["quantity"] == 1.0
    r = await eng.execute_entry(_sig(symbol="SOLUSDT"))                                 # nothing left
    assert r["reason"].startswith("rail_max_gross_leverage")


async def test_a_stop_past_bankruptcy_is_refused_before_any_order(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings={**SETTINGS, "leverage": 10, "isolated_margin": True})   # no rails
    r = await eng.execute_entry(_sig(stop_price=89.0))
    assert r["reason"].startswith("stop_past_bankruptcy") and "place_market_order" not in fake.names()
    assert (await eng.execute_entry(_sig(stop_price=95.0)))["success"]
    rows = await _rows(db, "SELECT count(*) AS n FROM positions")
    assert rows[0]["n"] == 1


async def test_a_stop_past_bankruptcy_is_fine_under_cross_margin(db):
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "leverage": 10, "isolated_margin": False})
    assert (await eng.execute_entry(_sig(stop_price=89.0)))["success"]


async def test_a_clip_below_the_venue_minimum_names_the_rail(db):
    eng, _ = await _engine(db, FakeBybit(), settings=_rails(max_notional_usd=0.5))     # < 1 lot at 100
    r = await eng.execute_entry(_sig())
    assert r["reason"] == "rail_max_notional_usd" and r["sizing"]["quantity"] == 0


async def test_two_entries_at_once_cannot_both_use_the_leverage_left(db):
    import asyncio
    settings = {**_rails(max_gross_leverage=0.08), "max_positions": None, "max_exposure_pct": None}   # the rail alone
    eng, _ = await _engine(db, FakeBybit(), settings=settings)        # 800 of 10000
    real = eng.risk_manager._open_exposure

    async def slow_read(*a):                     # the book is read, then the other entry gets a turn
        result = await real(*a)
        for _ in range(50):
            await asyncio.sleep(0)
        return result
    eng.risk_manager._open_exposure = slow_read
    a, b = await asyncio.gather(eng.execute_entry(_sig()), eng.execute_entry(_sig(symbol="ETHUSDT")))
    assert a["success"] and b["success"]
    assert sorted([a["sizing"]["quantity"], b["sizing"]["quantity"]]) == [3.0, 5.0]   # 500 + 300, not 500 + 500
