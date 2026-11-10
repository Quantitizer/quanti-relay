"""Risk rails (docs/SPEC.md §5): the deployment's own limits on every new entry.

Each rail accepts the entry, clips it (shrinks the order to fit) or rejects it with a reason. They
are configured in the deployment file under `rails:` (relay.deployment_config.Rails); absent ones
are off. The relay presets no limit.

  symbols.allow / symbols.deny   which symbols may be entered (reject)
  trading_hours                  when entries are allowed (reject outside; closes always work)
  max_notional_usd               one position's value, in USD (clip or reject)
  max_notional_pct               one position's value, as % of account equity (clip or reject)
  max_gross_leverage             all open positions' value plus this order, as a multiple of
                                 equity (clip or reject). Positions count at their ENTRY value
                                 (one that has gained counts at less than it is worth now), and
                                 the order at the expected price (a fill can slip a little past
                                 a clipped limit).
  on_limit                       clip (default: shrink the order to fit) or reject
  stop_before_bankruptcy         with a leverage set and isolated margin: refuse a stop at or past
                                 the price where the position's margin is gone (entry -/+
                                 entry/leverage). The venue liquidates a little BEFORE that price,
                                 so a stop close to it can still be too far; keep stops well
                                 inside. Under cross margin the whole balance backs a position, so
                                 this price means nothing there and the rail does not apply.

Pure functions, no I/O. The engine applies them in its entry path.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def symbol_refusal(rails: dict[str, Any] | None, unified: str) -> str | None:
    """Why `unified` (e.g. BTC/USDT:USDT) may not be entered, or None."""
    symbols = (rails or {}).get("symbols") or {}
    if unified in (symbols.get("deny") or ()):
        return f"{unified} is on the deny list"
    allow = symbols.get("allow")
    if allow and unified not in allow:
        return f"{unified} is not on the allow list"
    return None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def outside_trading_hours(rails: dict[str, Any] | None, now: datetime) -> str | None:
    """Why `now` (aware) is outside the deployment's trading hours, or None (inside, or no window).
    A window whose end is before its start runs overnight (22:00-06:00); its days are the days it
    starts on."""
    window = (rails or {}).get("trading_hours")
    if not window:
        return None
    local = now.astimezone(ZoneInfo(window.get("timezone") or "UTC"))
    start, end = _minutes(window["start"]), _minutes(window["end"])
    minute = local.hour * 60 + local.minute
    days = window.get("days") or DAYS
    today, yesterday = DAYS[local.weekday()], DAYS[(local.weekday() - 1) % 7]
    if start < end:
        inside = today in days and start <= minute < end
    else:                                            # overnight: from start today, or until end
        inside = (today in days and minute >= start) or (yesterday in days and minute < end)
    if inside:
        return None
    return (f"outside the trading hours ({','.join(days)} {window['start']}-{window['end']} "
            f"{window.get('timezone') or 'UTC'})")


def notional_cap(rails: dict[str, Any] | None, equity: float, open_notional: float) -> tuple[float, str] | None:
    """(the largest value this order may have, the rail that sets it), or None when no limit is set.
    The value is never negative: a book already at its leverage limit leaves 0."""
    r = rails or {}
    caps = []
    if r.get("max_notional_usd") is not None:
        caps.append((float(r["max_notional_usd"]), "max_notional_usd"))
    if r.get("max_notional_pct") is not None:
        caps.append((equity * float(r["max_notional_pct"]) / 100, "max_notional_pct"))
    if r.get("max_gross_leverage") is not None:
        caps.append((max(0.0, equity * float(r["max_gross_leverage"]) - open_notional), "max_gross_leverage"))
    return min(caps) if caps else None


def stop_past_bankruptcy(rails: dict[str, Any] | None, direction: str, entry: float, stop: float | None,
                         leverage: float | None, isolated: bool | None) -> str | None:
    """Why a stop at `stop` is at or past the bankruptcy price for `leverage`, or None (fine, no
    leverage known, not isolated margin, no stop, or the rail switched off)."""
    if (not (rails or {}).get("stop_before_bankruptcy", True) or isolated is not True or not leverage
            or not stop or entry <= 0):
        return None
    long_side = direction == "long"
    bankrupt = entry * (1 - 1 / leverage) if long_side else entry * (1 + 1 / leverage)
    eps = entry * 1e-9                       # "at" the price, despite float rounding (100 * 1.1 > 110)
    if (stop <= bankrupt + eps) if long_side else (stop >= bankrupt - eps):
        return (f"the stop {stop:g} is at or past {bankrupt:g}, where a {leverage:g}x {direction}'s margin "
                f"is gone (the venue liquidates before it): the stop would never fire")
    return None
