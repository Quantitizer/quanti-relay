"""Deployment file: one deployment's trading settings and risk rails, as YAML (docs/SPEC.md §5).

    # config/deployments/main.yaml
    name: main                    # optional; defaults to the file name
    enabled: true                 # false = no new entries (closes, stops and the emergency stop still work)
    dry_run: true                 # paper trading; see below
    paper_equity:                 # USDT the paper account pretends to hold, when there are no exchange keys
    leverage:                     # unset = the account's own leverage is used, never changed
    isolated_margin:              # true = isolated, false = cross, unset = never touched
    require_stop: true
    risk:
      max_positions:
      max_exposure_pct:
      daily_loss_limit_pct:
      min_balance:
      cooldown_hours:
      min_stop_distance_pct:
      max_stop_distance_pct:
      max_size_shortfall_pct:
    position_management:
      take_profit_pct:
      take_profit_ladder:         # e.g. [{pct: <PERCENT>, close_pct: <PERCENT>}, {pct: <PERCENT>, close_pct: <PERCENT>}]
      trailing_distance_pct:
      trailing_activate_pct:
      break_even_trigger_pct:
      break_even_trigger_r:
      break_even_trigger_tp:
      break_even_buffer_pct:
      max_hold_hours:             # the time stop: close a position open this long, at market
    rails:                        # relay.rails: each one off unless set
      symbols: {allow: [], deny: []}    # e.g. allow: [BTC/USDT:USDT, ETHUSDT]
      max_notional_usd:
      max_notional_pct:
      max_gross_leverage:
      on_limit: clip              # clip (shrink the order to fit) or reject
      trading_hours:              # e.g. {days: [mon, tue, wed, thu, fri], start: "<HH:MM>", end: "<HH:MM>", timezone: UTC}
      stop_before_bankruptcy: true  # isolated margin only
    venue:                        # where it trades
      exchange: bybit             # bybit (the native connector) or a CCXT exchange id, e.g. okx
      environment:                # for a CCXT exchange, REQUIRED: demo, testnet or live
    target:                       # target (whole-book) signals
      neutralize: false           # trim the larger side so long value == short value
      min_change_pct:             # hold same-side changes smaller than this % of equity (empty = trade any)

Every number is the user's own: the relay ships none, and an empty one means that rule is off.

Paper or live: the deployment trades on paper when EITHER this file or the environment setting
(RELAY_DRY_RUN) says dry_run, so going live takes both set to false by the owner.

With a deployment file, the trading settings come from it alone: the RELAY_* trading settings in the
environment are then ignored (the relay warns if any is set). Without one, they apply as before.
Only one deployment file is supported so far. A file the relay cannot use stops the start.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from relay.rails import DAYS
from relay.signals.contract import TakeProfitLevel, unified_symbol

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

# The environment settings a deployment file replaces (warned about when set alongside a file).
TRADING_ENV_SETTINGS = (
    "default_leverage", "isolated_margin", "require_stop", "max_positions", "max_exposure_pct",
    "daily_loss_limit_pct", "min_balance", "cooldown_hours", "min_stop_distance_pct",
    "max_stop_distance_pct", "take_profit_pct", "take_profit_ladder", "trailing_distance_pct",
    "trailing_activate_pct", "break_even_trigger_pct", "break_even_trigger_r", "break_even_buffer_pct",
    "break_even_trigger_tp", "max_size_shortfall_pct", "max_hold_hours",
)


class DeploymentConfigError(ValueError):
    """A deployment file the relay will not run with (the message says which and why)."""


class _UniqueKeys(yaml.SafeLoader):
    """YAML that refuses a key given twice (by default the last one silently wins: a pasted
    `dry_run: false` further down would quietly undo the one above)."""


def _mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"{key!r} is set twice", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeys.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(text: str) -> Any:
    """Safe YAML, refusing duplicate keys (deployment and source files)."""
    return yaml.load(text, Loader=_UniqueKeys)


def _empty_is_default(v: Any) -> Any:
    # A section whose lines are all left out or commented ("risk:") reads as None: = all off.
    return {} if v is None else v


class Risk(BaseModel):
    model_config = _STRICT
    max_positions: int | None = Field(default=None, ge=1)
    max_exposure_pct: float | None = Field(default=None, gt=0)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=100)
    min_balance: float | None = Field(default=None, ge=0)
    cooldown_hours: float | None = Field(default=None, ge=0)
    min_stop_distance_pct: float | None = Field(default=None, ge=0, lt=100)
    max_stop_distance_pct: float | None = Field(default=None, gt=0, lt=100)
    max_size_shortfall_pct: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _stop_band(self):
        lo, hi = self.min_stop_distance_pct, self.max_stop_distance_pct
        if lo is not None and hi is not None and lo >= hi:
            raise ValueError("min_stop_distance_pct must be smaller than max_stop_distance_pct")
        return self


class PositionManagement(BaseModel):
    model_config = _STRICT
    take_profit_pct: float | None = Field(default=None, gt=0)
    take_profit_ladder: list[TakeProfitLevel] | None = Field(default=None, min_length=1)
    trailing_distance_pct: float | None = Field(default=None, gt=0, lt=100)
    trailing_activate_pct: float | None = Field(default=None, ge=0)
    break_even_trigger_pct: float | None = Field(default=None, gt=0)
    break_even_trigger_r: float | None = Field(default=None, gt=0)
    break_even_trigger_tp: int | None = Field(default=None, ge=1)
    break_even_buffer_pct: float | None = Field(default=None, ge=0)
    max_hold_hours: float | None = Field(default=None, gt=0)      # the time stop (relay.execution.engine)

    @model_validator(mode="after")
    def _usable(self):
        # The same checks an entry would apply: an unusable rule stops the start, rather than
        # refusing every entry later.
        from relay.execution.engine import break_even_problem, trailing_problem
        from relay.execution.take_profit_ladder import ladder_problem
        ladder = [lv.model_dump(exclude_none=True) for lv in self.take_profit_ladder or []]
        problem = (
            ('set take_profit_pct or take_profit_ladder, not both'
             if self.take_profit_pct and ladder else None)
            or (ladder_problem(ladder) if ladder else None)
            or trailing_problem(self.trailing_distance_pct, self.trailing_activate_pct)
            or break_even_problem(self.break_even_trigger_pct, self.break_even_trigger_r,
                                  self.break_even_buffer_pct, self.break_even_trigger_tp)
        )
        if problem is None and self.break_even_trigger_tp is not None and len(ladder) < self.break_even_trigger_tp:
            problem = f'break_even_trigger_tp {self.break_even_trigger_tp} needs a take_profit_ladder with that level'
        if problem:
            raise ValueError(problem)
        return self


class Symbols(BaseModel):
    model_config = _STRICT
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _empty_list(cls, v: Any) -> Any:
        return [] if v is None else v

    @field_validator("allow", "deny")
    @classmethod
    def _unified(cls, v: list[str]) -> list[str]:
        return [unified_symbol(x) for x in v]          # any accepted spelling; unreadable ones stop the start


class TradingHours(BaseModel):
    model_config = _STRICT
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=lambda: list(DAYS), min_length=1)
    start: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$", description="HH:MM")
    end: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$", description="HH:MM")
    timezone: str = "UTC"

    @field_validator("start", "end", mode="before")
    @classmethod
    def _quoted(cls, v: Any) -> Any:
        if not isinstance(v, str):
            # YAML reads an unquoted 20:00 as the number 1200 (base 60).
            raise ValueError('write the time in quotes, e.g. "20:00"')  # noqa: TRY004  (pydantic reports ValueError)
        return v

    @model_validator(mode="after")
    def _window(self):
        if self.start == self.end:
            raise ValueError("start and end must differ (leave trading_hours out for always)")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {self.timezone!r} (e.g. UTC, Europe/Berlin)") from None
        return self


class Rails(BaseModel):
    model_config = _STRICT
    symbols: Symbols = Field(default_factory=Symbols)
    max_notional_usd: float | None = Field(default=None, gt=0)
    max_notional_pct: float | None = Field(default=None, gt=0)
    max_gross_leverage: float | None = Field(default=None, gt=0)
    on_limit: Literal["clip", "reject"] = "clip"
    trading_hours: TradingHours | None = None
    stop_before_bankruptcy: bool = True

    _sections = field_validator("symbols", mode="before")(_empty_is_default)


class TargetBook(BaseModel):
    """How target (whole-book) signals are carried out (relay.target_book)."""
    model_config = _STRICT
    neutralize: bool = False                                    # trim the larger side: long value == short value
    min_change_pct: float | None = Field(default=None, gt=0)    # hold same-side changes smaller than this % of equity


class Venue(BaseModel):
    """Where the deployment trades. Bybit is the native connector, whose host comes from the
    environment as before (RELAY_BYBIT_DEMO / RELAY_TESTNET); any other exchange goes through CCXT
    (relay.venue.ccxt_venue) and must say which of its environments: there is no default, so live
    trading is never chosen by leaving a setting out."""
    model_config = _STRICT
    exchange: str = Field(default="bybit", pattern=r"^[a-z0-9]{2,30}$")
    environment: Literal["demo", "testnet", "live"] | None = None
    # perpetual: USDT-settled perpetual futures (long and short). spot: buying coins with USDT and
    # selling them again - long only, no leverage, through CCXT (Bybit's spot too).
    market: Literal["perpetual", "spot"] = "perpetual"
    # Spot only, and required there: the owner confirms that this account (or sub-account) holds
    # only what the relay buys. On spot a position is the coins the account holds, so every coin
    # held there is taken for one of the relay's positions, managed and sold by it.
    dedicated_account: bool = False

    @field_validator("market", "dedicated_account", mode="before")
    @classmethod
    def _empty(cls, v: Any, info) -> Any:
        # Left empty in the file ("market:"): the default.
        return cls.model_fields[info.field_name].default if v is None else v

    @property
    def native(self) -> bool:
        """Bybit's own connector (Bybit perpetuals); everything else goes through CCXT."""
        return self.exchange == "bybit" and self.market == "perpetual"

    @property
    def venue_id(self) -> str:
        """The id positions and accounts are recorded under (okx, or okxspot for OKX's spot)."""
        return self.exchange + ("spot" if self.market == "spot" else "")

    @model_validator(mode="after")
    def _environment(self):
        if self.native and self.environment is not None:
            raise ValueError("for bybit the host comes from RELAY_BYBIT_DEMO / RELAY_TESTNET in the environment: "
                             "leave venue.environment empty")
        if not self.native and self.environment is None:
            raise ValueError(f"venue.environment is required for {self.venue_id}: demo, testnet or live")
        if self.market == "spot":
            if len(self.venue_id) > 30:
                raise ValueError(f"{self.exchange}: the exchange id is too long for spot (at most 26 characters)")
            if not self.dedicated_account:
                raise ValueError("venue.market spot needs venue.dedicated_account: true - on spot every coin the "
                                 "account holds is taken for one of the relay's positions (and sold by it), so "
                                 "use an account or sub-account that holds nothing else")
        elif self.dedicated_account:
            raise ValueError("venue.dedicated_account is for spot only: leave it out")
        return self


class DeploymentFile(BaseModel):
    model_config = _STRICT
    name: str = Field(min_length=1, max_length=50)
    enabled: bool = True
    dry_run: bool = True
    paper_equity: float | None = Field(default=None, gt=0)   # USDT, for paper without exchange keys
    leverage: float | None = Field(default=None, gt=0)
    isolated_margin: bool | None = None
    require_stop: bool = True
    risk: Risk = Field(default_factory=Risk)
    position_management: PositionManagement = Field(default_factory=PositionManagement)
    rails: Rails = Field(default_factory=Rails)
    target: TargetBook = Field(default_factory=TargetBook)
    venue: Venue = Field(default_factory=Venue)

    _sections = field_validator("risk", "position_management", "rails", "target", "venue",
                                mode="before")(_empty_is_default)

    def execution_settings(self) -> dict[str, Any]:
        """The engine's settings (the keys RELAY_* environment settings fill without a file)."""
        pm = self.position_management
        return {
            "enabled": self.enabled,
            "leverage": self.leverage,
            "isolated_margin": self.isolated_margin,
            "require_stop": self.require_stop,
            **self.risk.model_dump(),
            **pm.model_dump(exclude={"take_profit_ladder"}),
            "take_profit_ladder": [lv.model_dump(exclude_none=True) for lv in pm.take_profit_ladder]
            if pm.take_profit_ladder else None,
            "rails": self.rails.model_dump(),
            "target": self.target.model_dump(),
        }


def load_deployment(config_dir: str | Path) -> DeploymentFile | None:
    """The deployment in `<config_dir>/deployments/*.yaml`, or None when there is none (the
    environment settings then apply). Raises DeploymentConfigError for a file it will not run with,
    and for more than one file (several deployments are not supported yet)."""
    folder = Path(config_dir) / "deployments"
    if not folder.is_dir():
        return None
    files = sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")])
    if not files:
        return None
    if len(files) > 1:
        raise DeploymentConfigError(f"{folder}: {len(files)} deployment files ({', '.join(f.name for f in files)}); "
                                    f"only one deployment is supported so far")
    (path,) = files
    try:
        raw = read_yaml(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise DeploymentConfigError(f"{path}: not valid YAML ({e})") from e
    if not isinstance(raw, dict):
        raise DeploymentConfigError(f"{path}: expected a mapping of settings")
    raw.setdefault("name", path.stem)
    try:
        dep = DeploymentFile.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'file'}: {err['msg']}" for err in e.errors())
        raise DeploymentConfigError(f"{path}: {problems}") from None
    logger.info(f"[Deployment] {dep.name} from {path}: enabled={dep.enabled} dry_run={dep.dry_run}")
    return dep


def env_settings_ignored(cfg) -> list[str]:
    """The RELAY_* trading settings set in the environment that a deployment file overrides."""
    defaults = type(cfg).model_fields
    return [k for k in TRADING_ENV_SETTINGS if getattr(cfg, k) != defaults[k].default]
