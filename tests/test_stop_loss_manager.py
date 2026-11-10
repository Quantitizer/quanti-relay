import asyncio

import pytest

from relay.execution.stop_loss_manager import StopLossManager


class _FakeBybit:
    def __init__(self, responses=None):
        self.stop_calls = []
        self.cancel_calls = []
        self._responses = list(responses or [])

    async def set_trading_stop(self, **kw):
        self.stop_calls.append(kw)
        return self._responses.pop(0) if self._responses else {"retCode": 0}

    async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        self.cancel_calls.append((symbol, order_id))
        return {}


def _mgr(dry_run=False, responses=None):
    fake = _FakeBybit(responses)
    return StopLossManager(fake, dry_run=dry_run), fake


# ---- validate_stop: checks, never moves ------------------------------------

def test_valid_long_stop_is_returned_as_given():
    m, _ = _mgr()
    r = m.validate_stop("long", entry_price=100, stop_price=97, current_price=100)
    assert r == {"stop_price": 97, "valid": True, "issue": None, "distance_pct": 3.0, "warnings": []}


@pytest.mark.parametrize("side, stop, current", [
    ("long", 101, 100),     # above the price
    ("long", 100, 100),     # at the price
    ("long", 95, 93),       # the price fell through it
    ("short", 99, 100),
    ("short", 105, 106),
])
def test_a_breached_stop_is_reported_not_invented(side, stop, current):
    m, _ = _mgr()
    r = m.validate_stop(side, entry_price=100, stop_price=stop, current_price=current, max_distance_pct=3)
    assert not r["valid"] and r["issue"] == "stop_already_breached"
    assert r["stop_price"] == stop                       # never moved, never swapped for the price


def test_distance_limits_are_warnings_and_the_stop_is_kept():
    m, _ = _mgr()
    tight = m.validate_stop("long", entry_price=100, stop_price=99.8, current_price=100, min_distance_pct=0.5)
    assert tight["valid"] and tight["stop_price"] == 99.8 and tight["warnings"][0].startswith("below_min_distance")
    wide = m.validate_stop("short", entry_price=100, stop_price=120, current_price=100, max_distance_pct=10)
    assert wide["valid"] and wide["stop_price"] == 120 and wide["warnings"][0].startswith("beyond_max_distance")


def test_a_profit_protecting_stop_past_entry_is_valid():
    m, _ = _mgr()
    r = m.validate_stop("long", entry_price=100, stop_price=104, current_price=110, min_distance_pct=0.5)
    assert r["valid"] and r["stop_price"] == 104


def test_dry_run_never_calls_the_exchange():
    m, fake = _mgr(dry_run=True)
    r = asyncio.run(m.place_stop_loss("BTCUSDT", "long", 1, 95.0))
    assert r["success"] and fake.stop_calls == []
    assert asyncio.run(m.cancel_stop_loss("abc", "BTCUSDT")) is True
    assert fake.cancel_calls == []


def test_dry_run_flag_can_be_an_async_callable():
    state = {"dry": True}

    async def is_dry():
        return state["dry"]

    fake = _FakeBybit()
    m = StopLossManager(fake, dry_run=is_dry)
    asyncio.run(m.place_stop_loss("BTCUSDT", "long", 1, 95.0))
    assert fake.stop_calls == []
    state["dry"] = False
    asyncio.run(m.place_stop_loss("BTCUSDT", "long", 1, 95.0))
    assert len(fake.stop_calls) == 1


def test_place_sets_mark_price_full_stop_then_take_profit_separately():
    m, fake = _mgr()
    r = asyncio.run(m.place_stop_loss("BTCUSDT", "long", 1, 95.0, take_profit=110.0))
    assert r["success"] and "take_profit_error" not in r
    stop, tp = fake.stop_calls
    assert stop["stop_loss"] == 95.0 and "take_profit" not in stop
    assert stop["tpsl_mode"] == "Full" and stop["sl_trigger_by"] == "MarkPrice"
    assert tp["take_profit"] == 110.0 and "stop_loss" not in tp


def test_a_rejected_take_profit_never_costs_the_stop():
    m, fake = _mgr(responses=[{"retCode": 0}, {"retCode": 10001, "retMsg": "TakeProfit invalid"}])
    r = asyncio.run(m.place_stop_loss("BTCUSDT", "long", 1, 95.0, take_profit=90.0))
    assert r["success"] and r["take_profit_error"] == "TakeProfit invalid"
    assert fake.stop_calls[0]["stop_loss"] == 95.0


def test_not_modified_means_already_in_place_and_never_clears_the_stop():
    """'Not modified' = the requested stop is already set. Clearing and re-setting would leave the
    position unprotected in between (and for good if the second call failed)."""
    m, fake = _mgr(responses=[{"retCode": 34040, "retMsg": "not modified"}, {"retCode": 0}, {"retCode": 0}])
    r = asyncio.run(m.place_stop_loss("BTCUSDT", "long", 1, 95.0))
    assert r["success"] and r["unchanged"]
    assert [c.get("stop_loss") for c in fake.stop_calls] == [95.0]


def test_rejected_stop_reports_failure():
    m, fake = _mgr(responses=[{"retCode": 10001, "retMsg": "params error"}])
    r = asyncio.run(m.place_stop_loss("BTCUSDT", "long", 1, 95.0))
    assert not r["success"] and r["error"] == "params error"


def test_cancel_passes_symbol_and_order_id():
    """Regression: the predecessor called cancel_order(orderId=...) without a symbol -> TypeError,
    swallowed as a failed cancel on every call."""
    m, fake = _mgr()
    assert asyncio.run(m.cancel_stop_loss("abc-1", "ETHUSDT")) is True
    assert fake.cancel_calls == [("ETHUSDT", "abc-1")]


def test_cancel_synthetic_trading_stop_id_is_a_noop():
    m, fake = _mgr()
    assert asyncio.run(m.cancel_stop_loss("trading_stop_BTCUSDT", "BTCUSDT")) is True
    assert fake.cancel_calls == []
