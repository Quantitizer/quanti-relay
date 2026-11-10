"""Account-DD guardian gate logic (static max + trailing daily). All limits in percent."""
from datetime import UTC, datetime

import pytest

from relay.safety.account_dd_guardian import AccountDDGuardian, GuardianConfig

CFG = GuardianConfig(initial_balance=10_000, daily_dd_pct=5, max_dd_pct=10, dd_buffer_pct=0.5)


def _t(day, hour=12):
    return datetime(2026, 7, day, hour, 0, tzinfo=UTC)


def test_floors_with_buffer():
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    assert g.max_floor() == pytest.approx(9_050)   # 10000*(1-0.095)
    assert g.daily_floor() == pytest.approx(9_550)  # peak 10000 - 0.045*10000


def test_no_breach_above_floors():
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    v = g.observe(9_600, _t(1))
    assert not v.breach and not g.halted


def test_daily_breach_triggers_flatten_and_halt():
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    v = g.observe(9_500, _t(1))  # <= daily floor 9550
    assert v.breach and v.breach_type == "daily_dd"
    assert g.halted


def test_max_breach_takes_precedence():
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    v = g.observe(9_000, _t(1))  # <= max floor 9050 (also < daily) -> max_dd wins
    assert v.breach_type == "max_dd"


def test_trailing_daily_binds_after_intraday_peak():
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    g.observe(10_800, _t(1))            # intraday peak ratchets up
    assert g.daily_floor() == pytest.approx(10_350)  # 10800 - 450
    assert not g.observe(10_400, _t(1)).breach
    v = g.observe(10_300, _t(1))        # give back past the trailed floor
    assert v.breach and v.breach_type == "daily_dd"


def test_daily_peak_resets_next_utc_day():
    g = AccountDDGuardian(CFG)
    g.observe(10_800, _t(1))            # day 1 peak 10800
    g.reset()                           # clear halt for the test
    v = g.observe(10_500, _t(2))        # new UTC day -> peak resets to 10500
    assert g.daily_floor() == pytest.approx(10_050)  # 10500 - 450
    assert not v.breach


def test_daily_reset_rearms_daily_but_not_max():
    # daily-DD halt -> daily_reset re-arms
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    g.observe(9_500, _t(1))                 # daily breach
    assert g.halted and g.halt_type == "daily_dd"
    assert g.daily_reset(equity=9_600) is True
    assert not g.halted

    # max-DD halt -> daily_reset does NOT re-arm (permanent, account-ending)
    g2 = AccountDDGuardian(CFG)
    g2.observe(10_000, _t(1))
    g2.observe(9_000, _t(1))                # max breach
    assert g2.halt_type == "max_dd"
    assert g2.daily_reset(equity=9_000) is False
    assert g2.halted                        # still halted next day


def test_observe_self_heals_daily_halt_on_new_day():
    # A daily-DD halt clears on the first observe of a new UTC day (robust to a missed 00:00 cron),
    # but a max_dd halt is NEVER self-healed (lifetime).
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    assert g.observe(9_500, _t(1)).breach_type == "daily_dd" and g.halted
    v = g.observe(9_800, _t(2))                  # new day, healthy -> self-heal
    assert not g.halted and g.halt_type is None and not v.breach

    g2 = AccountDDGuardian(CFG)
    g2.observe(10_000, _t(1))
    g2.observe(9_000, _t(1))                      # max breach
    assert g2.halt_type == "max_dd"
    g2.observe(9_600, _t(2))                      # new day -> NOT cleared (lifetime)
    assert g2.halted and g2.halt_type == "max_dd"


def test_daily_halt_escalates_to_max_on_max_floor_cross():
    # A daily-DD halt that later crosses the STATIC max floor must ESCALATE to max_dd (lifetime),
    # so the 00:00 reset can never re-arm a blown account.
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    assert g.observe(9_500, _t(1)).breach_type == "daily_dd" and g.halt_type == "daily_dd"
    g.observe(9_000, _t(1))                      # <= max floor 9050 -> escalate
    assert g.halt_type == "max_dd"
    assert g.daily_reset(equity=9_000) is False  # lifetime, never re-armed
    assert g.halted


def test_state_roundtrip_survives_restart():
    # A lifetime max-DD halt must survive a process restart via to_state/load_state.
    g = AccountDDGuardian(CFG)
    g.observe(9_000, datetime(2026, 7, 10, 12, tzinfo=UTC))   # <= max floor -> halt
    assert g.halted and g.halt_type == "max_dd"
    g2 = AccountDDGuardian(CFG)                                        # "restart"
    g2.load_state(g.to_state())
    assert g2.halted and g2.halt_type == "max_dd"                     # halt persisted


def test_reset_refuses_permanent_max_halt_without_force():
    g = AccountDDGuardian(CFG)
    g.observe(10_000, _t(1))
    g.observe(9_000, _t(1))                 # max breach
    g.reset()                               # refused
    assert g.halted
    g.reset(force=True)                     # explicit admin override
    assert not g.halted


@pytest.mark.parametrize("kw", [
    {"daily_dd_pct": 0.05, "max_dd_pct": 0.10, "initial_balance": 0},      # no balance
    {"initial_balance": 10_000},                                         # no floor at all
    {"initial_balance": 10_000, "max_dd_pct": 150},                      # not a percent
    {"initial_balance": 10_000, "daily_dd_pct": 5, "dd_buffer_pct": 5},  # buffer eats the floor
    {"initial_balance": 10_000, "max_dd_pct": 10, "dd_buffer_pct": -1},
])
def test_configs_that_could_never_or_would_always_fire_are_refused(kw):
    with pytest.raises(ValueError):
        GuardianConfig(**kw)


def test_limits_are_percent_like_every_other_limit():
    """5 means 5%: the old fraction units made 5 a 500% floor that could never fire."""
    g = AccountDDGuardian(GuardianConfig(initial_balance=10_000, max_dd_pct=5))
    assert g.max_floor() == pytest.approx(9_500)
    assert g.observe(9_400, _t(1)).breach_type == "max_dd"


def test_each_floor_is_optional():
    only_daily = AccountDDGuardian(GuardianConfig(initial_balance=10_000, daily_dd_pct=5))
    assert only_daily.max_floor() is None
    assert only_daily.observe(10_000, _t(1)).breach is False
    assert only_daily.observe(9_400, _t(1)).breach_type == "daily_dd"


@pytest.mark.parametrize("kw", [{"initial_balance": float("nan"), "max_dd_pct": 10},
                                {"initial_balance": 10_000, "max_dd_pct": float("nan")},
                                {"initial_balance": 10_000, "max_dd_pct": 10, "dd_buffer_pct": float("inf")}])
def test_nan_and_infinity_are_refused(kw):
    with pytest.raises(ValueError):
        GuardianConfig(**kw)
