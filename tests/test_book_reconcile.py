"""Book reconciliation — the pure diff between the DB book and the exchange."""
from relay.execution.book_reconcile import reconcile


def _pos(size, side):
    return {"size": size, "side": side}


def test_reconcile_all_match():
    db = {"BTCUSDT": _pos(1.0, "long"), "ETHUSDT": _pos(2.0, "short")}
    ex = {"BTCUSDT": _pos(1.0, "long"), "ETHUSDT": _pos(2.0, "short")}
    r = reconcile(db, ex)
    assert r.safe_to_rebalance and r.matched == ["BTCUSDT", "ETHUSDT"] and not r.stale_db


def test_reconcile_flags_stale_mismatch_flip_untracked():
    db = {"A": _pos(1.0, "long"), "B": _pos(1.0, "long"), "C": _pos(1.0, "long"), "D": _pos(1.0, "long")}
    ex = {"A": _pos(1.0, "long"),                          # match
          "B": _pos(0.5, "long"),                          # size mismatch
          "C": _pos(1.0, "short"),                         # side flip
          # D absent -> stale
          "E": _pos(1.0, "long")}                          # untracked on exchange
    r = reconcile(db, ex)
    assert "D" in r.stale_db
    assert any(m[0] == "B" for m in r.size_mismatch)
    assert "C" in r.side_flip
    assert r.untracked_exch == ["E"]


def test_reconcile_aborts_on_large_drift():
    # 3 of 4 DB positions stale -> 75% drift > 25% -> not safe
    db = {s: _pos(1.0, "long") for s in "ABCD"}
    ex = {"A": _pos(1.0, "long")}
    r = reconcile(db, ex)
    assert not r.safe_to_rebalance and "abort" in r.reason
