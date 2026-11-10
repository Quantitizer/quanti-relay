"""Prop-program account rules — pure logic. The numbers are test examples, not any program's rules."""
from datetime import UTC, datetime

import pytest

from relay.safety.prop_rules import (
    ClosedTrade,
    PropRules,
    QualifyingDayTracker,
    SymbolMeta,
    aggregate_low_cap_ok,
    compliant_stop_distance_pct,
    is_low_cap,
    max_stop_distance_pct,
    position_risk_pct,
    required_native_stop,
    stop_price_for,
    violates_risk_cap,
)

CFG = PropRules(
    initial_balance=10_000.0,
    max_position_risk_pct=2.0, stop_distance_pct=12.0,
    min_qualifying_days=4, qualifying_notional_pct=4.0, qualifying_pnl_pct=0.5,
    low_cap_min_turnover_usd=3_000_000.0, low_cap_max_market_cap_usd=50_000_000.0,
    exclude_pre_listing=True, min_listing_age_days=14.0, low_cap_max_aggregate_pct=4.0,
)
OFF = PropRules(initial_balance=10_000.0)


# ---- config ----------------------------------------------------------------------

def test_every_rule_is_off_by_default():
    assert max_stop_distance_pct(5_000.0, OFF) == float("inf")
    assert violates_risk_cap(100.0, 50.0, 9_000.0, OFF) is False
    assert QualifyingDayTracker(OFF).days_short() == 0
    assert is_low_cap(SymbolMeta("X", turnover_24h_usd=1.0, market_cap_usd=1.0, listing_age_days=0), OFF)[0] is False
    assert aggregate_low_cap_ok([1e9], OFF) is True


def test_from_mapping_rejects_typos_and_skips_none():
    r = PropRules.from_mapping({"initial_balance": 25_000, "max_position_risk_pct": None})
    assert r.initial_balance == 25_000 and r.max_position_risk_pct is None
    with pytest.raises(ValueError, match="max_position_risk"):
        PropRules.from_mapping({"initial_balance": 1, "max_position_risk": 2.0})


# ---- native stop / risk cap --------------------------------------------------------

def test_max_stop_distance_caps_risk():
    # notional = 20% of initial -> 2% risk allows a 10% stop distance exactly
    assert max_stop_distance_pct(0.20 * CFG.initial_balance, CFG) == pytest.approx(10.0)


def test_compliant_stop_uses_tighter_of_desired_and_cap():
    big = 0.50 * CFG.initial_balance  # cap = 4%
    assert compliant_stop_distance_pct(big, CFG) == pytest.approx(4.0)
    assert 4.0 < CFG.stop_distance_pct, "fixture no longer exercises the cap branch"
    small = 0.03 * CFG.initial_balance  # cap ~66.7%, so the desired stop binds
    assert compliant_stop_distance_pct(small, CFG) == pytest.approx(CFG.stop_distance_pct)


def test_zero_notional_gives_infinite_headroom():
    assert max_stop_distance_pct(0.0, CFG) == float("inf")
    assert compliant_stop_distance_pct(0.0, CFG) == CFG.stop_distance_pct


def test_cap_alone_still_yields_a_stop_and_nothing_configured_raises():
    only_cap = PropRules(initial_balance=10_000.0, max_position_risk_pct=2.0)
    assert compliant_stop_distance_pct(2_000.0, only_cap) == pytest.approx(10.0)
    with pytest.raises(ValueError):
        compliant_stop_distance_pct(2_000.0, OFF)


def test_stop_price_direction():
    assert stop_price_for(100.0, "long", 12.0) == pytest.approx(88.0)
    assert stop_price_for(100.0, "short", 12.0) == pytest.approx(112.0)
    assert stop_price_for(100.0, "Buy", 10.0) == pytest.approx(90.0)


def test_required_native_stop_never_exceeds_risk_cap():
    for notional in (300.0, 2000.0, 5000.0, 9000.0):
        r = required_native_stop(100.0, "long", notional, CFG)
        assert r["risk_pct"] <= CFG.max_position_risk_pct + 1e-6
        assert 0 < r["stop_price"] < 100.0


def test_position_risk_pct_and_violation():
    # $5000 position, 10% stop -> risk = 0.10*5000/10000 = 5% > 2% cap
    assert position_risk_pct(100.0, 90.0, 5000.0, CFG) == pytest.approx(5.0)
    assert violates_risk_cap(100.0, 90.0, 5000.0, CFG) is True
    assert violates_risk_cap(100.0, 96.0, 5000.0, CFG) is False  # exactly 2%


# ---- qualifying trading days -------------------------------------------------------

def _t(day, notional, pnl):
    return ClosedTrade("X", notional, pnl, datetime(2026, 1, day, 12, 0, tzinfo=UTC))


def test_qualifying_trade_needs_both_notional_and_pnl():
    trk = QualifyingDayTracker(CFG)
    assert trk.qualifying_notional_floor() == pytest.approx(400.0)  # 4% of 10k
    assert trk.is_qualifying_trade(300.0, 100.0) is False   # below the notional floor
    assert trk.is_qualifying_trade(600.0, 2.0) is False     # 0.33% of notional
    assert trk.is_qualifying_trade(600.0, -3.0) is True     # 0.5%: a loss counts too
    assert trk.is_qualifying_trade(600.0, 3.0) is True


def test_pnl_is_measured_against_the_trade_not_the_account():
    # $3 on $600 is 0.5% of the trade but only 0.03% of the account -> qualifies
    assert QualifyingDayTracker(CFG).is_qualifying_trade(600.0, 3.0) is True


def test_tracker_counts_distinct_days_and_shortfall():
    trk = QualifyingDayTracker(CFG)
    for day in (1, 2, 3):
        trk.record_close(_t(day, 600.0, 10.0))     # qualifying
        trk.record_close(_t(day, 100.0, 50.0))     # noise, same day
    assert (trk.qualifying_days, trk.trading_days, trk.days_short()) == (3, 3, 1)


def test_needs_qualifying_trade():
    trk = QualifyingDayTracker(CFG)
    dt = datetime(2026, 1, 10, 9, 0, tzinfo=UTC)
    assert trk.needs_qualifying_trade(dt.date()) is True
    trk.record_close(ClosedTrade("X", 600.0, 10.0, dt))          # today qualified
    assert trk.needs_qualifying_trade(dt.date()) is False
    for day in range(1, 5):                                       # minimum met
        trk.record_close(_t(day, 600.0, 10.0))
    assert trk.needs_qualifying_trade(datetime(2026, 1, 20, tzinfo=UTC).date()) is False


def test_qualifying_state_survives_a_restart():
    t = QualifyingDayTracker(CFG)
    for day in (1, 2, 3):
        t.record_close(_t(day, 600.0, 10.0))
    t2 = QualifyingDayTracker.from_state(CFG, t.to_state())
    assert t2.qualifying_days == 3 and t2.days_short() == 1
    assert t2.needs_qualifying_trade(datetime(2026, 1, 1, tzinfo=UTC).date()) is False
    t3 = QualifyingDayTracker(CFG)
    t3.load_state(None)
    assert t3.qualifying_days == 0


# ---- low-cap exclusion -------------------------------------------------------------

def test_innovation_zone_is_low_cap_when_known():
    m = SymbolMeta("NEWUSDT", turnover_24h_usd=50_000_000, innovation_zone=True)
    assert is_low_cap(m, CFG) == (True, "innovation_zone")


def test_market_cap_applies_only_when_known():
    assert is_low_cap(SymbolMeta("S", turnover_24h_usd=50_000_000, market_cap_usd=40_000_000), CFG)[0] is True
    assert is_low_cap(SymbolMeta("B", turnover_24h_usd=50_000_000, market_cap_usd=None), CFG)[0] is False


def test_turnover_floor_excludes_thin_names():
    thin = SymbolMeta("THINUSDT", turnover_24h_usd=2_000_000)
    lc, reason = is_low_cap(thin, CFG)
    assert lc is True and "turnover" in reason
    assert is_low_cap(SymbolMeta("BTCUSDT", turnover_24h_usd=2_000_000_000), CFG)[0] is False


def test_pre_listing_and_listing_age_proxy():
    assert is_low_cap(SymbolMeta("N", turnover_24h_usd=50_000_000, is_pre_listing=True), CFG)[0] is True
    assert is_low_cap(SymbolMeta("F", turnover_24h_usd=50_000_000, listing_age_days=5), CFG)[0] is True
    assert is_low_cap(SymbolMeta("O", turnover_24h_usd=50_000_000, listing_age_days=400), CFG)[0] is False


def test_known_large_mcap_suppresses_age_proxy():
    m = SymbolMeta("BIGUSDT", turnover_24h_usd=500_000_000, market_cap_usd=5_000_000_000, listing_age_days=3)
    assert is_low_cap(m, CFG)[0] is False
    unknown_mcap = SymbolMeta("BIGUSDT", turnover_24h_usd=500_000_000, market_cap_usd=None, listing_age_days=3)
    lc, reason = is_low_cap(unknown_mcap, CFG)
    assert lc is True and "mcap-unknown" in reason


def test_aggregate_low_cap_cap():
    # 4% of 10k = $400 aggregate budget
    assert aggregate_low_cap_ok([150.0, 200.0], CFG) is True
    assert aggregate_low_cap_ok([250.0, 200.0], CFG) is False


def test_a_risk_budget_bigger_than_the_position_gives_no_negative_long_stop():
    cfg = PropRules(10_000, max_position_risk_pct=1)              # $100 budget, $50 position
    with pytest.raises(ValueError):
        required_native_stop(100.0, "long", 50.0, cfg)
    assert required_native_stop(100.0, "short", 50.0, cfg)["stop_price"] == pytest.approx(300.0)


@pytest.mark.parametrize("kw", [{"initial_balance": 0}, {"initial_balance": -5},
                                {"initial_balance": 1000, "max_position_risk_pct": -1}])
def test_invalid_rules_fail_loudly(kw):
    with pytest.raises(ValueError):
        PropRules(**kw)


def test_no_rule_is_on_unless_set():
    """The pre-listing exclusion is opt-in like every other rule."""
    assert is_low_cap(SymbolMeta("N", turnover_24h_usd=50_000_000, is_pre_listing=True), OFF)[0] is False


@pytest.mark.parametrize("kw", [{"initial_balance": float("nan")},
                                {"initial_balance": 1000, "max_position_risk_pct": 0},
                                {"initial_balance": 1000, "stop_distance_pct": 0},
                                {"initial_balance": 1000, "low_cap_max_aggregate_pct": float("nan")}])
def test_values_that_would_silently_misbehave_are_refused(kw):
    with pytest.raises(ValueError):
        PropRules(**kw)
