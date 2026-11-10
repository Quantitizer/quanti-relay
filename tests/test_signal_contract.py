"""Signal contract v1: the models every source's JSON is read with, and the schema generated from them."""
import math

import pytest
from pydantic import ValidationError

from relay.signals.contract import SCHEMA_PATH, parse_signal, render_schema, symbol_parts

ENV = {"version": "1", "id": "sig-1", "source": "mybot", "deployment": "default",
       "sent_at": "2026-11-02T08:00:00Z"}
OPEN = {**ENV, "kind": "event", "action": "open", "symbol": "BTC/USDT:USDT", "side": "long",
        "size": {"notional_usd": 500}, "stop": {"price": 95}}


def _bad(body, match):
    with pytest.raises(ValidationError, match=match):
        parse_signal(body)


@pytest.mark.parametrize("alias", ["BTC/USDT:USDT", "BTCUSDT", "btcusdt", "BTCUSDT.P", "BYBIT:BTCUSDT.P",
                                   " BTCUSDT "])
def test_symbol_aliases_read_as_one_perpetual(alias):
    assert symbol_parts(alias) == ("BTC", "USDT", "USDT")


def test_symbols_with_digits_and_other_quotes():
    assert symbol_parts("1000PEPEUSDT") == ("1000PEPE", "USDT", "USDT")
    assert symbol_parts("ETH/USDC:USDC") == ("ETH", "USDC", "USDC")


@pytest.mark.parametrize("bad", ["BTC/USDT", "USDT", "BTC-USDT", "BTCEUR", "BTCUSD", "", "BTC/USDT:USDT:X",
                                 "\u017folusdt", "b\u0131tusdt", "\uff11\uff10\uff10\uff10PEPEUSDT", "BTC_USDT"])
def test_unreadable_or_spot_symbols_are_refused(bad):
    with pytest.raises(ValueError):
        symbol_parts(bad)


def test_an_open_event_is_read_and_its_symbol_unified():
    s = parse_signal({**OPEN, "symbol": "BYBIT:BTCUSDT.P", "trailing": {"distance_pct": 1},
                      "take_profit": [{"pct": 2, "close_pct": 50}], "break_even": False,
                      "execution": {"type": "market"}})
    assert (s.kind, s.action, s.symbol, s.side, s.size) == ("event", "open", "BTC/USDT:USDT", "long",
                                                              {"notional_usd": 500})
    assert s.ttl_s == 60 and s.dry_run is None and s.break_even is False
    assert s.sent_at.utcoffset().total_seconds() == 0


def test_a_stop_as_a_percentage():
    assert parse_signal({**OPEN, "stop": {"pct": 2.5}}).stop.pct == 2.5
    _bad({**OPEN, "stop": {"pct": 2, "price": 95}}, "exactly one of price, pct")
    _bad({**OPEN, "stop": {"pct": 100}}, "less than 100")


@pytest.mark.parametrize("change, match", [
    ({"side": None}, "open needs a side"),
    ({"size": None}, "open needs a size"),
    ({"size": {"close_pct": 50}}, "open needs a size"),
    ({"size": {"notional_usd": 5, "qty": 1}}, "open needs a size"),
    ({"size": {"qty": 0}}, "size must be more than 0"),
    ({"size": {"qty": True}}, "valid number"),
    ({"symbol": None}, "open needs a symbol"),
    ({"symbol": "BTC/USDT"}, "spot pair"),
    ({"take_profit": []}, "at least one level"),
    ({"execution": {"type": "limit"}}, "price belongs to"),
    ({"execution": {"type": "market", "window_s": 5}}, "window_s belongs to"),
    ({"stop_price": 95}, "Extra inputs"),                         # a misspelt field is refused
    ({"ttl_s": "60"}, "valid number"),
    ({"version": 1}, "'1'"),
    ({"sent_at": "2026-11-02T08:00:00"}, "timezone"),             # no time zone: ambiguous
    ({"sent_at": "yesterday"}, "sent_at"),
    ({"stop": {"price": math.nan}}, "finite"),
    ({"kind": "news"}, "does not match any of the expected tags"),
])
def test_open_events_that_are_refused(change, match):
    _bad({k: v for k, v in {**OPEN, **change}.items() if v is not None}, match)


@pytest.mark.parametrize("body, match", [
    ({"action": "reduce", "symbol": "BTCUSDT"}, "reduce needs a size"),
    ({"action": "reduce", "symbol": "BTCUSDT", "size": {"close_pct": 0}}, "more than 0 and at most 100"),
    ({"action": "reduce", "symbol": "BTCUSDT", "size": {"close_pct": 150}}, "more than 0 and at most 100"),
    ({"action": "reduce", "symbol": "BTCUSDT", "size": {"close_pct": 50}, "side": "long"}, "side does not belong"),
    ({"action": "close", "symbol": "BTCUSDT", "size": {"qty": 1}}, "size does not belong"),
    ({"action": "close", "symbol": "BTCUSDT", "stop": {"price": 95}}, "stop does not belong"),
    ({"action": "close", "symbol": "BTCUSDT", "take_profit": False}, "take_profit does not belong"),
    ({"action": "close", "symbol": "BTCUSDT", "execution": {"type": "market"}}, "execution does not belong"),
    ({"action": "move_stop", "symbol": "BTCUSDT"}, "move_stop needs a stop"),
    ({"action": "move_stop", "symbol": "BTCUSDT", "stop": {"pct": 2}}, "move_stop needs a stop"),
    ({"action": "flip", "symbol": "BTCUSDT", "size": {"qty": 1}}, "flip needs a side"),
    ({"action": "liquidate", "symbol": "BTCUSDT"}, "action"),
])
def test_other_actions_take_only_their_own_fields(body, match):
    _bad({**ENV, "kind": "event", **body}, match)


def test_the_other_actions_in_their_simplest_form():
    ev = {**ENV, "kind": "event"}
    assert parse_signal({**ev, "action": "close", "symbol": "ETHUSDT"}).symbol == "ETH/USDT:USDT"
    assert parse_signal({**ev, "action": "close", "symbol": "ETHUSDT", "side": "short"}).side == "short"
    assert parse_signal({**ev, "action": "close_all"}).symbol is None
    assert parse_signal({**ev, "action": "reduce", "symbol": "ETHUSDT", "size": {"close_pct": 50}}).size == \
        {"close_pct": 50}
    assert parse_signal({**ev, "action": "move_stop", "symbol": "ETHUSDT", "stop": {"price": 2000}}).stop.price == 2000
    flip = parse_signal({**ev, "action": "flip", "symbol": "ETHUSDT", "side": "short", "size": {"equity_pct": 2},
                         "stop": {"pct": 3}})
    assert (flip.side, flip.stop.pct) == ("short", 3)


@pytest.mark.parametrize("field, value", [("id", ""), ("id", "x" * 101), ("source", ""), ("deployment", ""),
                                          ("ttl_s", 0), ("dry_run", "yes")])
def test_envelope_fields_are_checked(field, value):
    _bad({**OPEN, field: value}, field)


def test_a_target_signal_reads_its_weights_as_unified_symbols():
    s = parse_signal({**ENV, "kind": "target", "weights": {"BTCUSDT": 0.12, "ETH/USDT:USDT": -0.08},
                      "stop_pct": 3})
    assert s.weights == {"BTC/USDT:USDT": 0.12, "ETH/USDT:USDT": -0.08}
    _bad({**ENV, "kind": "target", "weights": {"BTCUSDT": 0.1, "BTC/USDT:USDT": 0.2}}, "same symbol")
    _bad({**ENV, "kind": "target", "weights": {"BTCUSDT": 0.1}, "action": "open"}, "Extra inputs")


def test_the_published_schema_is_the_one_the_relay_reads_with():
    # Regenerate with: uv run python -m relay.signals.contract
    assert SCHEMA_PATH.read_text() == render_schema()
