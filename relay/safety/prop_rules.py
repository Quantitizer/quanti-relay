"""
Account rules that funded-trader ("prop") programs commonly impose — pure, side-effect-free checks.

Every threshold defaults to None, which turns that rule off. A deployment states its program's
numbers in its YAML; the relay ships no program's values. Three rule families:

  - Native stop + per-position risk: every position carries an exchange-side stop, and the loss if
    it is hit stays <= `max_position_risk_pct` of the INITIAL balance.
  - Qualifying trading days: a day counts when it has >= 1 closed trade whose position value is
    >= `qualifying_notional_pct` of initial balance (incl. leverage) AND whose realized PnL is
    >= `qualifying_pnl_pct` of THAT trade's own position value (a loss counts too).
  - Low-cap exclusion: a symbol is "low-cap" if any configured criterion trips (market cap, 24h
    turnover, innovation-zone flag, pre-listing, listing age), and aggregate low-cap exposure is
    capped.

Data note (low-cap): Bybit's linear-perp `instruments-info` exposes NO market cap and NO
innovation-zone flag. With Bybit data alone the turnover floor applies, and listing age stands in
as a conservative proxy for new names. `market_cap_usd` and `innovation_zone` are honoured when an
external feed populates them.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from typing import Any


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class PropRules:
    initial_balance: float

    # native stop / per-position risk
    max_position_risk_pct: float | None = None   # loss at the stop <= this % of INITIAL balance
    stop_distance_pct: float | None = None       # desired stop distance (% of entry); risk-capped

    # qualifying trading days
    min_qualifying_days: int | None = None
    qualifying_notional_pct: float | None = None  # % of initial balance, incl. leverage
    qualifying_pnl_pct: float | None = None       # % of the trade's OWN notional

    # low-cap exclusion
    low_cap_min_turnover_usd: float | None = None
    low_cap_max_market_cap_usd: float | None = None
    exclude_pre_listing: bool = False             # opt-in, like every other rule
    min_listing_age_days: float | None = None     # proxy for new names, used only when mcap is unknown
    low_cap_max_aggregate_pct: float | None = None

    def __post_init__(self):
        # A typo must fail loudly, never quietly switch a rule off (e.g. a 0 balance made every
        # risk check pass).
        if not (isinstance(self.initial_balance, (int, float)) and math.isfinite(self.initial_balance)
                and self.initial_balance > 0):
            raise ValueError(f"initial_balance must be a number > 0, got {self.initial_balance!r}")
        # A risk cap or stop distance of 0 would put the stop AT the entry: refused (unset = off).
        for f in ("max_position_risk_pct", "stop_distance_pct"):
            v = getattr(self, f)
            if v is not None and not (math.isfinite(v) and v > 0):
                raise ValueError(f"{f} must be > 0 when set, got {v!r}")
        for f in ("qualifying_notional_pct", "qualifying_pnl_pct", "low_cap_min_turnover_usd",
                  "low_cap_max_market_cap_usd", "min_listing_age_days", "low_cap_max_aggregate_pct"):
            v = getattr(self, f)
            if v is not None and not (math.isfinite(v) and v >= 0):
                raise ValueError(f"{f} must be >= 0, got {v!r}")
        if self.min_qualifying_days is not None and self.min_qualifying_days < 0:
            raise ValueError(f"min_qualifying_days must be >= 0, got {self.min_qualifying_days!r}")

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> PropRules:
        """Build from a deployment-config mapping. Unknown keys are rejected so a typo can't
        silently switch a rule off."""
        known = {f.name for f in fields(cls)}
        unknown = set(m) - known
        if unknown:
            raise ValueError(f"unknown prop rule(s): {sorted(unknown)}")
        return cls(**{k: v for k, v in m.items() if v is not None})


# ------------------------------------------------------------ native stop / risk cap
_LONG = {"long", "buy"}


def _is_long(side: str) -> bool:
    return str(side).strip().lower() in _LONG


def max_stop_distance_pct(notional: float, cfg: PropRules) -> float:
    """Largest stop distance (% of entry) that keeps risk <= max_position_risk_pct
    of INITIAL balance. Risk = distance_frac * notional. Inf when notional is 0 or no cap is set."""
    if notional <= 0 or cfg.max_position_risk_pct is None:
        return float("inf")
    return (cfg.max_position_risk_pct / 100.0) * cfg.initial_balance / notional * 100.0


def compliant_stop_distance_pct(notional: float, cfg: PropRules) -> float:
    """The stop distance to actually use: the tighter of the desired stop and the risk-cap-implied
    maximum. Raises if neither is configured — a mandatory stop needs a distance."""
    desired = cfg.stop_distance_pct if cfg.stop_distance_pct is not None else float("inf")
    dist = min(desired, max_stop_distance_pct(notional, cfg))
    if dist == float("inf"):
        raise ValueError("no stop distance: set stop_distance_pct or max_position_risk_pct")
    return dist


def stop_price_for(entry_price: float, side: str, stop_distance_pct: float) -> float:
    d = stop_distance_pct / 100.0
    return entry_price * (1.0 - d) if _is_long(side) else entry_price * (1.0 + d)


def required_native_stop(entry_price: float, side: str, notional: float,
                         cfg: PropRules) -> dict[str, float]:
    """The risk-capped native stop to attach at entry for one position.
    Returns {'stop_price', 'stop_distance_pct', 'risk_pct'}."""
    dist = compliant_stop_distance_pct(notional, cfg)
    if _is_long(side) and dist >= 100.0:
        # The risk budget exceeds the whole position: no price stop is implied (a long cannot fall
        # below 0). Refuse rather than return a negative or zero stop.
        raise ValueError(f"risk cap implies a {dist:.1f}% stop for this long; set stop_distance_pct")
    px = stop_price_for(entry_price, side, dist)
    return {
        "stop_price": px,
        "stop_distance_pct": dist,
        "risk_pct": position_risk_pct(entry_price, px, notional, cfg),
    }


def position_risk_pct(entry_price: float, stop_price: float, notional: float,
                      cfg: PropRules) -> float:
    """Risk of a stopped-out position as a % of INITIAL balance."""
    if entry_price <= 0 or cfg.initial_balance <= 0:
        return 0.0
    dist_frac = abs(entry_price - stop_price) / entry_price
    return dist_frac * notional / cfg.initial_balance * 100.0


def violates_risk_cap(entry_price: float, stop_price: float, notional: float,
                      cfg: PropRules) -> bool:
    if cfg.max_position_risk_pct is None:
        return False
    return position_risk_pct(entry_price, stop_price, notional, cfg) > cfg.max_position_risk_pct + 1e-9


# ------------------------------------------------------------- qualifying trading days
@dataclass
class ClosedTrade:
    symbol: str
    notional: float          # position value incl. leverage, USD
    realized_pnl: float      # USD (signed)
    close_time: datetime     # UTC (naive treated as UTC)


def _utc_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(UTC).date()


class QualifyingDayTracker:
    """Counts qualifying trading days (see the module doc) and how many are still owed."""

    def __init__(self, cfg: PropRules):
        self.cfg = cfg
        self._qualifying_days: set[date] = set()
        self._trading_days: set[date] = set()

    # ---- rule primitives ----
    def qualifying_notional_floor(self) -> float:
        return (self.cfg.qualifying_notional_pct or 0.0) / 100.0 * self.cfg.initial_balance

    def is_qualifying_trade(self, notional: float, realized_pnl: float) -> bool:
        if notional <= 0 or notional < self.qualifying_notional_floor() - 1e-9:
            return False
        pnl_pct_of_notional = abs(realized_pnl) / notional * 100.0
        return pnl_pct_of_notional >= (self.cfg.qualifying_pnl_pct or 0.0) - 1e-9

    # ---- accounting ----
    def record_close(self, trade: ClosedTrade) -> bool:
        """Record a closed trade. Returns True if it made the day qualify."""
        d = _utc_date(trade.close_time)
        self._trading_days.add(d)
        if self.is_qualifying_trade(trade.notional, trade.realized_pnl):
            was = d in self._qualifying_days
            self._qualifying_days.add(d)
            return not was
        return False

    @property
    def qualifying_days(self) -> int:
        return len(self._qualifying_days)

    @property
    def trading_days(self) -> int:
        return len(self._trading_days)

    def days_short(self) -> int:
        return max(0, (self.cfg.min_qualifying_days or 0) - self.qualifying_days)

    def needs_qualifying_trade(self, today: date) -> bool:
        """True if we still owe qualifying days AND today has not qualified yet."""
        if today in self._qualifying_days:
            return False
        return self.days_short() > 0

    # ---- persistence (survive restarts so the count isn't lost) ----
    def to_state(self) -> dict:
        return {"qualifying_days": sorted(d.isoformat() for d in self._qualifying_days),
                "trading_days": sorted(d.isoformat() for d in self._trading_days)}

    def load_state(self, state: dict | None) -> None:
        if not state:
            return
        self._qualifying_days = {date.fromisoformat(d) for d in state.get("qualifying_days", [])}
        self._trading_days = {date.fromisoformat(d) for d in state.get("trading_days", [])}

    @classmethod
    def from_state(cls, cfg: PropRules, state: dict | None) -> QualifyingDayTracker:
        t = cls(cfg)
        t.load_state(state)
        return t


# ------------------------------------------------------------------ low-cap exclusion
@dataclass(frozen=True)
class SymbolMeta:
    symbol: str
    turnover_24h_usd: float | None = None
    market_cap_usd: float | None = None     # None until an external feed provides it
    innovation_zone: bool | None = None     # None: Bybit perps don't expose it
    is_pre_listing: bool = False
    listing_age_days: float | None = None


def is_low_cap(meta: SymbolMeta, cfg: PropRules) -> tuple[bool, str]:
    """Low-cap predicate -> (is_low_cap, reason). Any single configured criterion trips it.

    Precedence, so real data always wins over proxies:
      1. Innovation-zone flag (if a source ever sets it True).
      2. Market cap: if KNOWN, it decides the cap dimension outright — the listing-age proxy is then
         NOT applied (it would false-positive on a brand-new perp contract of an established,
         large-cap coin). If mcap is UNKNOWN (feed down), the listing-age proxy is the conservative
         fallback for new names.
      3. 24h turnover floor — always applied.
      4. Pre-listing — always excluded (contract not fully trading).
    """
    if meta.innovation_zone is True:
        return True, "innovation_zone"
    if meta.market_cap_usd is not None:
        if cfg.low_cap_max_market_cap_usd is not None and meta.market_cap_usd < cfg.low_cap_max_market_cap_usd:
            return True, f"market_cap<{cfg.low_cap_max_market_cap_usd:.0f}"
        # mcap known -> real data; skip the age proxy entirely.
    else:
        # mcap unknown (feed unavailable) -> conservative age fallback for new names.
        if (cfg.min_listing_age_days is not None and meta.listing_age_days is not None
                and meta.listing_age_days < cfg.min_listing_age_days):
            return True, f"listing_age<{cfg.min_listing_age_days:.0f}d(mcap-unknown)"
    if (cfg.low_cap_min_turnover_usd is not None and meta.turnover_24h_usd is not None
            and meta.turnover_24h_usd < cfg.low_cap_min_turnover_usd):
        return True, f"turnover<{cfg.low_cap_min_turnover_usd:.0f}"
    if cfg.exclude_pre_listing and meta.is_pre_listing:
        return True, "pre_listing"
    return False, ""


def aggregate_low_cap_ok(low_cap_notionals: Iterable[float], cfg: PropRules) -> bool:
    """Aggregate low-cap exposure must stay <= low_cap_max_aggregate_pct of balance (if set)."""
    if cfg.low_cap_max_aggregate_pct is None:
        return True
    total = float(sum(low_cap_notionals))
    return total <= cfg.low_cap_max_aggregate_pct / 100.0 * cfg.initial_balance + 1e-6
