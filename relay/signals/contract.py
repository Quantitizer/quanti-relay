"""Signal contract v1 (docs/SPEC.md §3): the JSON every source sends, as typed models.

One envelope (version, id, source, deployment, sent_at, ttl_s, kind, dry_run) and two kinds:
  event   one action on one symbol: open, close, reduce, flip, close_all, move_stop
  target  a whole book of signed weights (executed from R3; accepted by the contract now)

`schemas/signal.v1.json` is generated from these models (`python -m relay.signals.contract`) and a
test keeps the two identical, so the schema a sender validates against is exactly what the relay
accepts. Every model is strict: unknown fields are refused (a misspelt "take_profit" must not be
silently ignored), and so are non-numbers where a number belongs (`true` is not 1) and Infinity/NaN.

Symbols are accepted in CCXT unified form (`BTC/USDT:USDT`) and in common aliases (`BTCUSDT`,
`BTCUSDT.P`, `BYBIT:BTCUSDT.P`); `symbol_parts` reads them all. The relay sets no trading number
here: every size, stop and take-profit is the sender's.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "signal.v1.json"

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

# Quote currencies an alias may end in: linear (stablecoin-settled) perpetuals. A coin-settled
# "BTCUSD" is an inverse contract, which the relay does not trade, so it is not read as one.
_QUOTES = ("USDT", "USDC")
_UNIFIED = re.compile(r"^([A-Z0-9]+)/([A-Z0-9]+)(?::([A-Z0-9]+))?$")


def symbol_parts(symbol: str) -> tuple[str, str, str]:
    """(base, quote, settle) of a perpetual symbol in CCXT unified form or a common alias.

    'BTC/USDT:USDT', 'btcusdt', 'BTCUSDT.P' and 'BYBIT:BTCUSDT.P' all give ('BTC', 'USDT', 'USDT').
    A unified symbol without its settle currency ('BTC/USDT') is a spot pair in CCXT and is refused:
    the relay trades perpetuals. Raises ValueError for anything it cannot read.
    """
    s = str(symbol).strip()
    if not s.isascii():
        # Unicode case rules would map other letters onto real symbols ('ſ' -> 'S').
        raise ValueError(f"unreadable symbol {symbol!r}: letters and digits A-Z, 0-9 only")
    s = s.upper()
    m = _UNIFIED.match(s)
    if m:
        base, quote, settle = m.groups()
        if not settle:
            raise ValueError(f"{symbol!r} is a spot pair; a perpetual is written like {base}/{quote}:{quote}")
        return base, quote, settle
    if ":" in s:                                   # 'BYBIT:BTCUSDT.P' - an exchange prefix
        s = s.split(":", 1)[1]
    s = s.removesuffix(".P")                       # TradingView's perpetual suffix
    for quote in _QUOTES:
        if s.endswith(quote) and s[:-len(quote)].isalnum():          # ASCII only (checked above)
            return s[:-len(quote)], quote, quote
    raise ValueError(f"unreadable symbol {symbol!r}: use the unified form, e.g. BTC/USDT:USDT")


def unified_symbol(symbol: str) -> str:
    base, quote, settle = symbol_parts(symbol)
    return f"{base}/{quote}:{settle}"


class Trailing(BaseModel):
    """A trailing stop: it follows the best price at distance_pct %, starting once the position is
    activate_pct % in profit from entry (omitted: at once). It runs beside the fixed stop."""
    model_config = _STRICT
    distance_pct: float = Field(gt=0, lt=100)
    activate_pct: float | None = Field(default=None, ge=0)


class TakeProfitLevel(BaseModel):
    """One take-profit level: at `price` (or `pct` % from entry) close `close_pct` % of the position."""
    model_config = _STRICT
    price: float | None = Field(default=None, gt=0)
    pct: float | None = Field(default=None, gt=0)
    close_pct: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _price_or_pct(self):
        if (self.price is None) == (self.pct is None):
            raise ValueError("set exactly one of price, pct")
        return self


class BreakEven(BaseModel):
    """Break-even: once the position is trigger_pct % in profit, or trigger_r times its initial risk
    (entry to the opening stop), or take-profit level trigger_tp has filled, its stop moves to the
    entry price plus buffer_pct % (omitted: exactly the entry). Set one trigger. It only ever
    tightens the stop."""
    model_config = _STRICT
    trigger_pct: float | None = Field(default=None, gt=0)
    trigger_r: float | None = Field(default=None, gt=0)
    trigger_tp: int | None = Field(default=None, ge=1)
    buffer_pct: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_trigger(self):
        if sum(t is not None for t in (self.trigger_pct, self.trigger_r, self.trigger_tp)) != 1:
            raise ValueError("set exactly one of trigger_pct, trigger_r, trigger_tp")
        if self.trigger_pct is not None and self.buffer_pct is not None and self.buffer_pct >= self.trigger_pct:
            raise ValueError("buffer_pct must be smaller than trigger_pct")
        return self


class Stop(BaseModel):
    """The stop: a price, or a % from the entry price (on the loss side)."""
    model_config = _STRICT
    price: float | None = Field(default=None, gt=0)
    pct: float | None = Field(default=None, gt=0, lt=100)

    @model_validator(mode="after")
    def _price_or_pct(self):
        if (self.price is None) == (self.pct is None):
            raise ValueError("set exactly one of price, pct")
        return self


class Execution(BaseModel):
    """How the order is executed. market: at once. limit: resting at `price`. maker_chase: post-only,
    following the best price for up to `window_s` seconds."""
    model_config = _STRICT
    type: Literal["market", "limit", "maker_chase"]
    price: float | None = Field(default=None, gt=0)
    window_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _fields_per_type(self):
        if (self.type == "limit") != (self.price is not None):
            raise ValueError("price belongs to (and is needed by) execution type limit")
        if (self.type == "maker_chase") != (self.window_s is not None):
            raise ValueError("window_s belongs to (and is needed by) execution type maker_chase")
        return self


_OPEN_SIZES = ("notional_usd", "equity_pct", "risk_pct", "qty")


class _Envelope(BaseModel):
    model_config = _STRICT
    version: Literal["1"]
    id: str = Field(min_length=1, max_length=100, description="sender-unique; a repeat is never executed twice")
    source: str = Field(min_length=1, max_length=50, description="the registered source sending this")
    deployment: str = Field(min_length=1, max_length=50, description="the deployment this signal targets")
    sent_at: AwareDatetime = Field(description="ISO-8601 with a time zone, e.g. 2026-11-02T08:00:00Z")
    ttl_s: float = Field(default=60, gt=0, description="refused as stale after sent_at + ttl_s seconds")
    dry_run: bool | None = Field(default=None, description="true = simulate this signal (only ever more cautious)")

    @field_validator("sent_at", mode="before")
    @classmethod
    def _iso_text(cls, v: Any) -> Any:
        # JSON carries timestamps as text; strict mode would refuse any text for a datetime.
        return datetime.fromisoformat(v) if isinstance(v, str) else v


class EventSignal(_Envelope):
    kind: Literal["event"]
    action: Literal["open", "close", "reduce", "flip", "close_all", "move_stop"]
    symbol: str | None = Field(default=None, max_length=40,
                               description="e.g. BTC/USDT:USDT (aliases: BTCUSDT, BTCUSDT.P, BYBIT:BTCUSDT.P)")
    side: Literal["long", "short"] | None = None
    size: dict[str, float] | None = Field(
        default=None, description="one of notional_usd, equity_pct, risk_pct, qty (open, flip); close_pct (reduce)")
    stop: Stop | None = None
    take_profit: list[TakeProfitLevel] | Literal[False] | None = Field(
        default=None, description="a take-profit ladder; false = none (not the deployment's)")
    trailing: Trailing | Literal[False] | None = None
    break_even: BreakEven | Literal[False] | None = None
    execution: Execution | None = None

    @field_validator("symbol")
    @classmethod
    def _readable_symbol(cls, v: str | None) -> str | None:
        return None if v is None else unified_symbol(v)

    @model_validator(mode="after")
    def _fields_per_action(self):
        a = self.action
        if a != "close_all" and self.symbol is None:
            raise ValueError(f"{a} needs a symbol")
        if a in ("open", "flip") and self.side is None:
            raise ValueError(f"{a} needs a side")
        if a not in ("open", "flip", "close") and self.side is not None:
            raise ValueError(f"side does not belong to {a}")
        if a in ("open", "flip"):
            if not self.size or len(self.size) != 1 or next(iter(self.size)) not in _OPEN_SIZES:
                raise ValueError(f"{a} needs a size with exactly one of {list(_OPEN_SIZES)}")
        elif a == "reduce":
            if not self.size or set(self.size) != {"close_pct"}:
                raise ValueError("reduce needs a size {\"close_pct\": ...}")
            if not 0 < self.size["close_pct"] <= 100:
                raise ValueError("close_pct must be more than 0 and at most 100")
        elif self.size is not None:
            raise ValueError(f"size does not belong to {a}")
        if self.size and not all(v > 0 for v in self.size.values()):
            raise ValueError("size must be more than 0")
        if a == "move_stop":
            if self.stop is None or self.stop.price is None:
                raise ValueError("move_stop needs a stop {\"price\": ...}")
        elif a not in ("open", "flip") and self.stop is not None:
            raise ValueError(f"stop does not belong to {a}")
        if a not in ("open", "flip"):
            for name in ("take_profit", "trailing", "break_even", "execution"):
                if getattr(self, name) is not None:
                    raise ValueError(f"{name} does not belong to {a}")
        if self.take_profit == []:
            raise ValueError("take_profit needs at least one level (false = none)")
        return self


class TargetSignal(_Envelope):
    kind: Literal["target"]
    weights: dict[str, float] = Field(description="signed fractions of deployment equity; symbols left out go to zero")
    gross_leverage: float | None = Field(default=None, gt=0)
    stop_pct: float | None = Field(default=None, gt=0, lt=100)
    execution: Execution | None = None

    @field_validator("weights")
    @classmethod
    def _unified_keys(cls, v: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for k, w in v.items():
            u = unified_symbol(k)
            if u in out:
                raise ValueError(f"{k!r} and another key are the same symbol {u}")
            out[u] = w
        return out


Signal = Annotated[EventSignal | TargetSignal, Field(discriminator="kind")]
_SIGNAL = TypeAdapter(Signal)


def parse_signal(body: dict[str, Any]) -> EventSignal | TargetSignal:
    """The signal in `body` (a decoded JSON object). Raises pydantic.ValidationError."""
    return _SIGNAL.validate_python(body)


def json_schema() -> dict[str, Any]:
    schema = _SIGNAL.json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Quanti Relay signal v1",
            **schema}


def render_schema() -> str:
    return json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    SCHEMA_PATH.parent.mkdir(exist_ok=True)
    SCHEMA_PATH.write_text(render_schema())
    print(f"wrote {SCHEMA_PATH}")
