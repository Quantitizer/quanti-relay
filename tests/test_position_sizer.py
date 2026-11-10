import pytest

from relay.execution.position_sizer import compute_size


def test_notional_usd():
    r = compute_size({"notional_usd": 500}, entry_price=100.0, equity=10_000)
    assert r.quantity == pytest.approx(5.0)
    assert r.notional == 500
    assert r.equity_pct == pytest.approx(5.0)
    assert not r.clipped


def test_equity_pct():
    r = compute_size({"equity_pct": 2}, entry_price=50.0, equity=10_000)
    assert r.notional == pytest.approx(200.0)
    assert r.quantity == pytest.approx(4.0)


def test_risk_pct_uses_stop_distance():
    # lose 0.5% of 10k = $50 if a 2% stop is hit -> notional $2,500
    r = compute_size({"risk_pct": 0.5}, entry_price=100.0, equity=10_000, stop_price=98.0)
    assert r.notional == pytest.approx(2_500.0)
    assert r.quantity == pytest.approx(25.0)


def test_risk_pct_short_stop_above_entry():
    r = compute_size({"risk_pct": 1}, entry_price=100.0, equity=10_000, stop_price=105.0)
    assert r.notional == pytest.approx(2_000.0)


def test_risk_pct_without_stop_is_skipped():
    r = compute_size({"risk_pct": 0.5}, entry_price=100.0, equity=10_000)
    assert r.quantity == 0.0
    assert "stop" in r.reason


def test_risk_pct_stop_equal_entry_is_skipped():
    r = compute_size({"risk_pct": 0.5}, entry_price=100.0, equity=10_000, stop_price=100.0)
    assert r.quantity == 0.0


def test_qty_passthrough():
    r = compute_size({"qty": 0.01}, entry_price=60_000.0, equity=0)
    assert r.quantity == pytest.approx(0.01)
    assert r.notional == pytest.approx(600.0)
    assert r.equity_pct == 0.0


def test_max_notional_clips():
    r = compute_size({"notional_usd": 5_000}, entry_price=100.0, equity=10_000, max_notional=1_000)
    assert r.notional == 1_000
    assert r.quantity == pytest.approx(10.0)
    assert r.clipped


def test_below_minimum_is_skipped():
    r = compute_size({"notional_usd": 5}, entry_price=100.0, equity=10_000, min_notional=10)
    assert r.quantity == 0.0
    assert "below minimum" in r.reason


def test_clip_below_minimum_is_skipped():
    r = compute_size({"notional_usd": 500}, entry_price=100.0, equity=10_000, max_notional=5, min_notional=10)
    assert r.quantity == 0.0


def test_no_minimum_is_assumed():
    """The venue's own minimum is checked later, by the engine; the sizer invents none."""
    assert compute_size({"notional_usd": 1}, entry_price=100.0, equity=10_000).quantity == pytest.approx(0.01)


@pytest.mark.parametrize("price", [0, -1, None])
def test_invalid_price(price):
    assert compute_size({"notional_usd": 500}, entry_price=price, equity=10_000).quantity == 0.0


@pytest.mark.parametrize("size", [{}, {"notional_usd": 1, "qty": 1}, {"lots": 3}])
def test_exactly_one_mode(size):
    assert compute_size(size, entry_price=100.0, equity=10_000).quantity == 0.0


@pytest.mark.parametrize("size", [{"qty": 0}, {"qty": -1}, {"qty": "abc"}, {"qty": None}])
def test_bad_values(size):
    assert compute_size(size, entry_price=100.0, equity=10_000).quantity == 0.0


@pytest.mark.parametrize("mode", ["equity_pct", "risk_pct"])
def test_equity_modes_need_equity(mode):
    r = compute_size({mode: 1}, entry_price=100.0, equity=0, stop_price=95.0)
    assert r.quantity == 0.0
