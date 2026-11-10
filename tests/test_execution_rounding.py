"""Exchange rounding helpers (pure)."""
import pytest

from relay.execution.rounding import (
    format_price,
    format_qty,
    round_price_to_tick,
    round_qty_to_step,
    round_stop_to_tick,
)


def test_qty_rounds_down_to_step():
    assert round_qty_to_step(1.2377, 0.001) == pytest.approx(1.237)   # down, never up
    assert round_qty_to_step(10.0, 0.01) == pytest.approx(10.0)


def test_qty_below_min_is_dust():
    assert round_qty_to_step(0.0004, 0.001, min_qty=0.001) == 0.0
    assert round_qty_to_step(0.0, 0.001) == 0.0


def test_stop_rounds_toward_entry_long_and_short():
    # long stop below entry -> round UP (tighter, less risk)
    assert round_stop_to_tick(85.03, 0.1, "long") == pytest.approx(85.1)
    # short stop above entry -> round DOWN (tighter)
    assert round_stop_to_tick(115.07, 0.1, "short") == pytest.approx(115.0)
    # already on a tick -> unchanged
    assert round_stop_to_tick(85.0, 0.1, "long") == pytest.approx(85.0)


def test_no_tick_or_step_passthrough():
    assert round_qty_to_step(1.2377, None) == pytest.approx(1.2377)
    assert round_stop_to_tick(85.03, 0, "long") == pytest.approx(85.03)
    assert round_price_to_tick(100.04, 0.1) == pytest.approx(100.0)


def test_format_qty_step_precision_no_artifacts():
    # F1: clean decimal string at the step's precision — no float artifact, no scientific notation.
    assert format_qty(5000.0000001, 0.1) == "5000.0"
    assert format_qty(0.30000000004, 0.001) == "0.300"
    assert format_qty(2750.5, 1) == "2750"        # integer step -> no decimals, rounded DOWN
    assert format_qty(1e-05, 1) == "0"            # never emits '1e-05'
    assert "e" not in format_qty(0.00000012, 0.00000001).lower()
    assert format_qty(4.0, 1000) == "0"           # below a coarse step -> 0 (dropped upstream)


def test_format_price_tick_precision_no_artifacts():
    assert format_price(0.00057500001, 0.000001) == "0.000575"
    assert format_price(85.10000001, 0.1) == "85.1"
    assert "e" not in format_price(0.00000575, 0.00000001).lower()
