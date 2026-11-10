"""Target book planning (relay.target_book): weights -> rails -> neutral -> venue minimums -> actions."""
import pytest

from relay.target_book import (
    Action,
    Leg,
    apply_rails,
    drop_below_minimum,
    execution_order,
    neutralize_net,
    plan_actions,
    target_notionals,
)

BTC, ETH, SOL, XRP = "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"


def test_weights_become_signed_values_of_equity():
    assert target_notionals({BTC: 0.1, ETH: -0.05, SOL: 0.0}, 10_000) == {BTC: 1000.0, ETH: -500.0}
    assert target_notionals({BTC: 0.1}, 10_000, gross_leverage=2) == {BTC: 2000.0}


def test_rails_drop_clip_and_scale_the_book():
    rails = {"symbols": {"allow": [], "deny": [SOL]}, "max_notional_usd": 1500, "max_notional_pct": None,
             "max_gross_leverage": 0.2}
    out, notes = apply_rails({BTC: 3000.0, ETH: -1000.0, SOL: 500.0}, rails, 10_000)
    # SOL denied; BTC clipped to 1500; gross 2500 > 2000 -> everything x0.8 (shape kept)
    assert out == {BTC: pytest.approx(1200.0), ETH: pytest.approx(-800.0)}
    assert len(notes) == 3 and any("SOL" in x and "deny" in x for x in notes)
    assert any("clipped" in x for x in notes) and notes[-1].startswith("book scaled by 0.8000")


def test_rails_by_percent_and_allow_list():
    rails = {"symbols": {"allow": [BTC, ETH], "deny": []}, "max_notional_pct": 5}
    out, _ = apply_rails({BTC: -900.0, ETH: 200.0, XRP: 100.0}, rails, 10_000)
    assert out == {BTC: -500.0, ETH: 200.0}                           # 5% of 10000; XRP not allowed


def test_no_rails_change_nothing():
    assert apply_rails({BTC: 3000.0}, None, 10_000) == ({BTC: 3000.0}, [])


@pytest.mark.parametrize("book, expected", [
    ({BTC: 1000.0, ETH: -500.0}, {BTC: 500.0, ETH: -500.0}),
    ({BTC: 300.0, ETH: -600.0, SOL: -300.0}, {BTC: 300.0, ETH: -200.0, SOL: -100.0}),
    ({BTC: 1000.0}, {BTC: 1000.0}),                                   # one-sided: left alone
    ({BTC: 500.0, ETH: -500.0}, {BTC: 500.0, ETH: -500.0}),
])
def test_neutralize_trims_the_larger_side(book, expected):
    assert neutralize_net(book) == pytest.approx(expected)


SPECS = {s: {"qty_step": 0.01, "min_qty": 0.01, "min_notional": 5} for s in (BTC, ETH, SOL)}


def test_legs_the_venue_cannot_execute_are_dropped_then_the_book_neutralised_again():
    prices = {BTC: 100.0, ETH: 100.0, SOL: 100.0}
    out, notes = drop_below_minimum({BTC: 600.0, ETH: -500.0, SOL: -4.0}, prices, SPECS, neutralize=True)
    assert out == pytest.approx({BTC: 500.0, ETH: -500.0}) and "SOL" in notes[0]
    out, _ = drop_below_minimum({BTC: 600.0, ETH: -500.0, SOL: -4.0}, prices, SPECS, neutralize=False)
    assert out == {BTC: 600.0, ETH: -500.0}


def test_legs_without_a_price_or_lot_rules_are_dropped():
    out, notes = drop_below_minimum({BTC: 600.0, XRP: 600.0}, {BTC: 100.0}, SPECS, neutralize=False)
    assert out == {BTC: 600.0} and "no price" in notes[0]


def test_a_leg_rounding_below_the_minimum_quantity_is_dropped():
    specs = {BTC: {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5}}
    out, _ = drop_below_minimum({BTC: 60.0}, {BTC: 78_000.0}, specs, neutralize=False)        # 0.00077 BTC
    assert out == {}


def _kinds(actions):
    return {a.symbol: a.action for a in actions}


def test_actions_against_the_book():
    current = {BTC: Leg("long", 10, 1000.0), ETH: Leg("long", 5, 500.0), SOL: Leg("short", 3, 300.0),
               XRP: Leg("long", 100, 50.0)}
    target = {BTC: 1500.0, ETH: -500.0, SOL: -300.0, "DOGE/USDT:USDT": 200.0}
    acts = plan_actions(target, current, 10_000, None)
    assert _kinds(acts) == {BTC: "resize", ETH: "flip", SOL: "hold", "DOGE/USDT:USDT": "enter", XRP: "exit"}
    (exit_,) = [a for a in acts if a.action == "exit"]
    assert (exit_.side, exit_.current_notional) == ("long", 50.0)


def test_small_changes_are_held_under_the_users_threshold():
    current = {BTC: Leg("long", 10, 1000.0)}
    assert _kinds(plan_actions({BTC: 1040.0}, current, 10_000, 0.5)) == {BTC: "hold"}     # 40 < 50
    assert _kinds(plan_actions({BTC: 1060.0}, current, 10_000, 0.5)) == {BTC: "resize"}   # 60 > 50
    assert _kinds(plan_actions({BTC: 1000.0001}, current, 10_000, None)) == {BTC: "resize"}
    assert _kinds(plan_actions({BTC: 1000.0}, current, 10_000, None)) == {BTC: "hold"}


def test_execution_frees_margin_first():
    acts = [Action("enter", "A", "long", 100, 0), Action("resize", "B", "long", 200, 100),
            Action("flip", "C", "short", 100, 100), Action("resize", "D", "long", 50, 100),
            Action("exit", "E", "long", 0, 100), Action("hold", "F", "long", 100, 100)]
    assert [a.symbol for a in execution_order(acts)] == ["E", "D", "C", "B", "A"]


def test_the_tighter_of_two_notional_limits_wins():
    out, _ = apply_rails({BTC: 3000.0}, {"max_notional_usd": 2000, "max_notional_pct": 5}, 10_000)   # 2000 vs 500
    assert out == {BTC: 500.0}
    out, _ = apply_rails({BTC: 3000.0}, {"max_notional_usd": 400, "max_notional_pct": 5}, 10_000)
    assert out == {BTC: 400.0}
    out, _ = apply_rails({BTC: 1000.0}, {"max_notional_usd": 2000, "max_notional_pct": 5}, 10_000)  # between
    assert out == {BTC: 500.0}


def test_a_leg_over_the_minimum_value_but_under_the_minimum_quantity_is_dropped():
    specs = {BTC: {"qty_step": 0.01, "min_qty": 1.0, "min_notional": 5}}
    out, _ = drop_below_minimum({BTC: 60.0}, {BTC: 100.0}, specs, neutralize=False)        # 0.6 < 1, 60 > 5
    assert out == {}


def test_neutralising_is_rechecked_until_every_leg_is_executable():
    specs = {s: {"qty_step": 0.01, "min_qty": 0.01, "min_notional": 50} for s in (BTC, ETH, SOL)}
    prices = {BTC: 100.0, ETH: 100.0, SOL: 100.0}
    # 600 / 400 long vs 100 short: neutral 60 / 40 - ETH (40) is then below 50: dropped, and again
    out, notes = drop_below_minimum({BTC: 600.0, ETH: 400.0, SOL: -100.0}, prices, specs, neutralize=True)
    assert out == pytest.approx({BTC: 60.0, SOL: -60.0}) and any("ETH" in n for n in notes)


def test_a_change_too_small_for_the_venue_is_held():
    specs = {BTC: {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5}}
    current = {BTC: Leg("long", 0.01, 780.0)}
    acts = plan_actions({BTC: 778.4}, current, 10_000, None, {BTC: 78_000.0}, specs)   # -1.6 USD: 0.00002 BTC
    assert _kinds(acts) == {BTC: "hold"}
    acts = plan_actions({BTC: 700.0}, current, 10_000, None, {BTC: 78_000.0}, specs)   # -80 USD: 0.001 BTC
    assert _kinds(acts) == {BTC: "resize"}
