"""Authoritative P&L reconciler — pure window-sum core."""
from datetime import UTC, datetime

from relay.execution.position_pnl_reconcile import (
    reconcile_row,
    sum_closed_pnl_in_window,
    sum_funding_in_window,
    to_ms,
)


def _cp(order_id, ms, pnl, exit_px=100.0):
    return {"orderId": order_id, "updatedTime": str(ms), "closedPnl": str(pnl), "avgExitPrice": str(exit_px)}


def _settle(symbol, ms, change):
    return {"type": "SETTLEMENT", "symbol": symbol, "transactionTime": str(ms), "change": str(change)}


OPEN = datetime(2026, 1, 10, 0, 5, tzinfo=UTC)
CLOSE = datetime(2026, 1, 10, 12, 5, tzinfo=UTC)
S, E = to_ms(OPEN), to_ms(CLOSE)


def test_window_sum_captures_trim_plus_final():
    # A partial trim mid-hold emits its OWN closed-pnl record; the window-sum must include it.
    recs = [
        _cp("trim1", S + 3_600_000, 12.5),   # +1h: the trim
        _cp("final", E - 60_000, 40.0),       # the final close
        _cp("other", E + 10_000_000, 999.0),  # way outside the window -> excluded
    ]
    r = sum_closed_pnl_in_window(recs, S, E + 120_000)
    assert r["n"] == 2 and abs(r["pnl"] - 52.5) < 1e-9
    assert r["order_ids"] == ["trim1", "final"] and r["exit_price"] == 100.0


def test_claimed_set_prevents_double_count():
    # Two overlapping rows for the same symbol must not both claim the same close record.
    recs = [_cp("close1", S + 1000, 10.0)]
    claimed = set()
    a = sum_closed_pnl_in_window(recs, S, E, claimed)
    b = sum_closed_pnl_in_window(recs, S, E, claimed)   # second row, same window
    assert a["pnl"] == 10.0 and b["pnl"] == 0.0 and b["n"] == 0


def test_funding_sum_by_symbol_and_window():
    # funding = Σ change of SETTLEMENT rows for THIS symbol in-window (received +, paid −).
    tx = [
        _settle("BTCUSDT", S + 100_000, -1.20),   # paid
        _settle("BTCUSDT", S + 200_000, 0.35),    # received
        _settle("ETHUSDT", S + 100_000, -5.00),   # different symbol -> excluded
        _settle("BTCUSDT", E + 10_000_000, -9.0), # out of window -> excluded
        {"type": "TRADE", "symbol": "BTCUSDT", "transactionTime": str(S + 100_000), "change": "-0.5"},  # not funding
    ]
    r = sum_funding_in_window(tx, "BTCUSDT", S, E + 120_000)
    assert r["n"] == 2 and abs(r["funding"] - (-0.85)) < 1e-9


def test_reconcile_row_total_is_trade_plus_funding():
    recs = [_cp("trim1", S + 3_600_000, 12.5), _cp("final", E - 60_000, 40.0)]
    tx = [_settle("BTCUSDT", S + 100_000, -1.20), _settle("BTCUSDT", S + 200_000, 0.35)]
    out = reconcile_row(recs, tx, "BTCUSDT", OPEN, CLOSE)
    assert out["source"] == "venue_api"
    assert abs(out["pnl"] - 52.5) < 1e-9
    assert abs(out["funding"] - (-0.85)) < 1e-9
    assert abs(out["total"] - 51.65) < 1e-9
    assert out["n_closes"] == 2 and out["n_funding"] == 2


def test_reconcile_row_no_closed_pnl_is_no_match_but_keeps_funding():
    tx = [_settle("BTCUSDT", S + 100_000, -1.20)]
    out = reconcile_row([], tx, "BTCUSDT", OPEN, CLOSE)
    assert out["source"] == "no_match" and out["pnl"] is None
    assert abs(out["funding"] - (-1.20)) < 1e-9   # funding still surfaced for diagnosis


def test_reconcile_row_missing_times_unresolvable():
    out = reconcile_row([_cp("x", S, 1.0)], [], "BTCUSDT", None, CLOSE)
    assert out["source"] == "unresolvable" and out["pnl"] is None
