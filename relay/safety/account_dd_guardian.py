"""NOT WIRED YET: the running relay does not use this module. It is kept for phase 2 (deployment
files with drawdown limits), and reviewed and fixed when it is connected (tests/test_unwired_modules.py
keeps the running relay from importing it before then).

Account-level drawdown guardian. Several deployments can share ONE exchange account, so this
account-wide monitor enforces drawdown floors against combined mark-to-market equity (incl.
unrealized PnL) and flattens EVERYTHING on the account before a hard breach.

Floors (anchored to the FIXED initial balance — the convention most prop-firm rules use). Every
number is the user's own, in percent like every other limit (5 = 5%), and each floor is optional:
  - max-DD  : STATIC floor = initial × (1 − max_dd_pct/100).
  - daily-DD: TRAILING floor = intraday_peak − daily_dd_pct/100 × initial, reset 00:00 UTC.
An optional `dd_buffer_pct` flattens that much EARLIER than each floor (slippage room).

`AccountDDGuardian` is pure and unit-tested; the loop that feeds it equity and flattens on a
breach is relay.safety.guardian_job.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)


def _finite_positive(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v) and v > 0


@dataclass(frozen=True)
class GuardianConfig:
    initial_balance: float
    daily_dd_pct: float | None = None   # % of initial balance, e.g. 5; None = no daily floor
    max_dd_pct: float | None = None     # % of initial balance, e.g. 10; None = no max floor
    dd_buffer_pct: float | None = None  # % of initial balance to flatten early; None = at the floor

    def __post_init__(self):
        # Fail loudly on a config that would silently never fire (or always fire).
        if not _finite_positive(self.initial_balance):
            raise ValueError(f"initial_balance must be a number > 0, got {self.initial_balance!r}")
        if self.daily_dd_pct is None and self.max_dd_pct is None:
            raise ValueError("a guardian needs daily_dd_pct and/or max_dd_pct")
        for name in ("daily_dd_pct", "max_dd_pct"):
            v = getattr(self, name)
            if v is not None and not (_finite_positive(v) and v < 100):
                raise ValueError(f"{name} is a percent (5 = 5%) and must be between 0 and 100, got {v!r}")
        buf = self.dd_buffer_pct
        if buf is not None:
            if not math.isfinite(buf) or buf < 0:
                raise ValueError(f"dd_buffer_pct must be >= 0, got {buf!r}")
            for name in ("daily_dd_pct", "max_dd_pct"):
                v = getattr(self, name)
                if v is not None and buf >= v:
                    raise ValueError(f"dd_buffer_pct ({buf}) must be smaller than {name} ({v}): "
                                     f"otherwise any dip flattens the account")


@dataclass
class GuardianVerdict:
    breach: bool                  # True = flatten the account now
    breach_type: str | None       # 'max_dd' | 'daily_dd' | None
    equity: float
    max_floor: float | None       # None = that floor is not configured
    daily_floor: float | None


def _utc_date(now: datetime) -> date:
    return (now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)).date()


class AccountDDGuardian:
    """Stateful gate: tracks the intraday equity peak (trailing daily) and the static
    max floor. `observe(equity, now)` returns a verdict; once breached it stays halted
    until `reset()`."""

    def __init__(self, cfg: GuardianConfig):
        self.cfg = cfg
        self._day: date | None = None
        self._intraday_peak: float | None = None
        self._halted = False
        self._halt_type: str | None = None   # 'max_dd' (permanent) | 'daily_dd' (re-arms 00:00 UTC)

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_type(self) -> str | None:
        return self._halt_type

    @property
    def intraday_peak(self) -> float | None:
        return self._intraday_peak

    def _effective_pct(self, pct: float) -> float:
        return (pct - (self.cfg.dd_buffer_pct or 0.0)) / 100.0

    def max_floor(self) -> float | None:
        if self.cfg.max_dd_pct is None:
            return None
        return self.cfg.initial_balance * (1.0 - self._effective_pct(self.cfg.max_dd_pct))

    def daily_floor(self) -> float | None:
        if self.cfg.daily_dd_pct is None:
            return None
        peak = self._intraday_peak if self._intraday_peak is not None else self.cfg.initial_balance
        return peak - self._effective_pct(self.cfg.daily_dd_pct) * self.cfg.initial_balance

    def observe(self, equity: float, now: datetime) -> GuardianVerdict:
        day = _utc_date(now)
        if day != self._day:                      # new UTC day -> reset trailing daily peak
            self._day = day
            self._intraday_peak = equity
            # Self-heal a daily-DD halt at the day boundary even if the 00:00 reset cron was missed
            # (e.g. an outage across midnight). NEVER clears a max_dd halt — that is lifetime.
            if self._halt_type == "daily_dd":
                self._halted = False
                self._halt_type = None
        if self._intraday_peak is None or equity > self._intraday_peak:
            self._intraday_peak = equity

        max_floor = self.max_floor()
        daily_floor = self.daily_floor()
        breach_type = None
        if max_floor is not None and equity <= max_floor:
            breach_type = "max_dd"
        elif daily_floor is not None and equity <= daily_floor:
            breach_type = "daily_dd"
        breach = breach_type is not None
        if breach:
            if not self._halted:
                self._halted = True
                self._halt_type = breach_type
            elif breach_type == "max_dd" and self._halt_type != "max_dd":
                # ESCALATE: a position that was daily-halted and then crosses the STATIC max floor is
                # lifetime-blown — promote to max_dd so daily_reset can never re-arm the account.
                self._halt_type = "max_dd"
        return GuardianVerdict(breach, breach_type, equity, max_floor, daily_floor)

    def daily_reset(self, equity: float | None = None) -> bool:
        """00:00-UTC re-arm. Re-arms ONLY a daily-DD halt; a max-DD halt is LIFETIME
        (account-ending) and is never cleared here. Resets the intraday peak for the new
        day regardless. Returns True if entries were re-enabled."""
        self._day = None
        self._intraday_peak = equity
        if self._halt_type == "max_dd":
            return False                      # permanent — stays halted
        self._halted = False
        self._halt_type = None
        return True

    # ---- persistence: survive restarts so a lifetime max-DD halt isn't forgotten ----
    def to_state(self) -> dict:
        return {"halted": self._halted, "halt_type": self._halt_type,
                "day": self._day.isoformat() if self._day else None,
                "intraday_peak": self._intraday_peak}

    def load_state(self, state: dict | None) -> None:
        if not state:
            return
        self._halted = bool(state.get("halted", False))
        self._halt_type = state.get("halt_type")
        d = state.get("day")
        self._day = date.fromisoformat(d) if d else None
        self._intraday_peak = state.get("intraday_peak")

    def reset(self, force: bool = False) -> None:
        """Full clear. By default refuses to clear a permanent max-DD halt (guards against
        accidental re-arm of a blown account); pass force=True for an explicit admin override."""
        if self._halt_type == "max_dd" and not force:
            logger.warning("[dd-guardian] reset() refused: max-DD halt is permanent (use force=True)")
            return
        self._halted = False
        self._halt_type = None
        self._day = None
        self._intraday_peak = None

