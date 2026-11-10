"""NOT WIRED YET: the running relay does not use this module. It is kept for phase 2 (several accounts per relay), and reviewed and fixed when it is connected (tests/test_unwired_modules.py keeps the running relay from importing it before then).


Book reconciliation — the DB `positions` can drift from the actual exchange positions (a stop
fired between signals, a failed record, a manual trade). A diff against a target book is only
correct if it starts from REALITY, so before acting on one we reconcile the deployment's book
against the exchange and TRUST THE EXCHANGE.

Pure diff + a `safe_to_rebalance` gate (abort if drift is too large — better a stale hold
than trading on a wrong picture). Repair actions are applied by the caller (mark stale rows
closed, correct sizes, adopt/flag untracked).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReconcileResult:
    matched: list[str] = field(default_factory=list)
    stale_db: list[str] = field(default_factory=list)        # OPEN in DB, absent on exchange -> close in DB
    size_mismatch: list[tuple] = field(default_factory=list)  # (sym, db_size, exch_size) -> correct DB
    side_flip: list[str] = field(default_factory=list)        # DB side != exchange side
    untracked_exch: list[str] = field(default_factory=list)   # on exchange, not in the DB book
    safe_to_rebalance: bool = True
    reason: str = ""


def reconcile(db_book: dict[str, dict], exch_positions: dict[str, dict],
              size_tol: float = 0.02, max_drift_frac: float = 0.25) -> ReconcileResult:
    """db_book/exch_positions: {symbol: {'size': float>0, 'side': 'long'|'short'}}.
    `size_tol` = relative size tolerance; `max_drift_frac` = if more than this fraction of the
    DB book is stale/mismatched, it is NOT safe to rebalance."""
    r = ReconcileResult()
    db_syms = set(db_book)
    exch_syms = set(exch_positions)

    for s in sorted(db_syms):
        d = db_book[s]
        e = exch_positions.get(s)
        if e is None or float(e.get("size") or 0) <= 0:
            r.stale_db.append(s)                       # DB says open, exchange says flat
            continue
        if str(d.get("side")) != str(e.get("side")):
            r.side_flip.append(s)
            continue
        ds, es = float(d.get("size") or 0), float(e.get("size") or 0)
        if es > 0 and abs(ds - es) / es > size_tol:
            r.size_mismatch.append((s, ds, es))
        else:
            r.matched.append(s)

    r.untracked_exch = sorted(exch_syms - db_syms)

    drift = len(r.stale_db) + len(r.size_mismatch) + len(r.side_flip)
    denom = max(len(db_syms), 1)
    if drift / denom > max_drift_frac:
        r.safe_to_rebalance = False
        r.reason = (f"book drift {drift}/{denom} > {max_drift_frac:.0%} — abort rebalance, "
                    f"repair DB first (stale={len(r.stale_db)}, mismatch={len(r.size_mismatch)}, "
                    f"flip={len(r.side_flip)})")
    return r
