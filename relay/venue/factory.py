"""Building the venue a deployment trades on (its `venue:` section) and checking that the
exchange can carry out what the deployment asks of it.

Keys come from the environment only, named after the exchange (with Docker, from .env; the relay itself does not read .env):

    RELAY_<EXCHANGE>_API_KEY, RELAY_<EXCHANGE>_API_SECRET
    RELAY_<EXCHANGE>_API_PASSPHRASE                 OKX, Bitget, KuCoin (their "passphrase")
    RELAY_<EXCHANGE>_WALLET_ADDRESS, RELAY_<EXCHANGE>_PRIVATE_KEY   Hyperliquid

e.g. RELAY_OKX_API_KEY. On paper (dry-run) no keys are needed: the relay then only reads prices
and market rules. Live trading without them is refused at start.
"""
from __future__ import annotations

import os
from typing import Any

from relay.venue.profiles import Profile, profile_for

# CCXT's flags for an exchange that takes stop (trigger) orders through its API.
STOP_FLAGS = ("createStopLossOrder", "createTriggerOrder", "createStopOrder", "createStopMarketOrder")

_ENV_NAMES = {"apiKey": "API_KEY", "secret": "API_SECRET", "password": "API_PASSPHRASE", "uid": "API_UID",
              "login": "API_LOGIN", "token": "API_TOKEN", "walletAddress": "WALLET_ADDRESS",
              "privateKey": "PRIVATE_KEY"}


class VenueConfigError(ValueError):
    """A venue the relay will not start with (the message says which and why)."""


def key_env_names(profile: Profile) -> dict[str, str]:
    """{CCXT credential: environment variable} for this exchange."""
    prefix = f"RELAY_{profile.exchange.upper()}_"
    return {cred: prefix + _ENV_NAMES[cred] for cred in profile.credentials}


def environments(profile: Profile, exchange_class) -> tuple[str, ...]:
    """The environments this exchange offers the relay: its profile's, or for an exchange without
    one, testnet when CCXT knows a test host. Live always."""
    if profile.environments:
        offered = profile.environments
    else:
        urls = getattr(exchange_class, "urls", None) or {}
        offered = ("testnet",) if urls.get("test") else ()
    return (*offered, "live")


def build_ccxt_venue(exchange: str, environment: str, *, paper: bool, environ=None, market: str = "perpetual"):
    """A ready CcxtVenue (markets not loaded yet: call prepare()). Raises VenueConfigError."""
    environ = os.environ if environ is None else environ
    try:
        import ccxt.async_support as ccxt_async
    except ImportError:
        from relay.plugins import installed
        if exchange in installed("venue"):
            return _build_plugin(exchange, environment, paper=paper, environ=environ, market=market)
        raise VenueConfigError(f"{exchange} needs the CCXT library: pip install \"quanti-relay[venues]\"") from None
    cls = getattr(ccxt_async, exchange, None)
    if cls is None or exchange not in getattr(ccxt_async, "exchanges", ()):
        return _build_plugin(exchange, environment, paper=paper, environ=environ, market=market)
    profile = profile_for(exchange)
    sample = cls()
    offered = environments(profile, sample)
    if environment not in offered:
        raise VenueConfigError(f"{exchange} offers no {environment} environment to the relay (it offers: "
                               f"{', '.join(offered)})")
    names = key_env_names(profile)
    creds = {cred: environ.get(var, "") for cred, var in names.items()}
    missing = [var for cred, var in names.items() if not creds[cred]]
    if missing and not paper:
        raise VenueConfigError(f"{exchange}: {', '.join(missing)} not set (keys are read from the environment only, "
                               f"not from the .env file)")
    # warnOnFetchOpenOrdersWithoutSymbol: Binance otherwise refuses to list every market's open
    # orders, which the emergency cancel-all needs.
    config: dict[str, Any] = {"enableRateLimit": True,
                              "options": {"defaultType": "spot" if market == "spot" else "swap",
                                          "warnOnFetchOpenOrdersWithoutSymbol": False}}
    if not missing:
        config.update(creds)
    x = cls(config)
    if environment == "testnet":
        x.set_sandbox_mode(True)
    elif environment == "demo":
        x.enable_demo_trading(True)
    if market == "spot":
        from relay.venue.ccxt_spot import CcxtSpotVenue
        return CcxtSpotVenue(x, profile)
    from relay.venue.ccxt_venue import CcxtVenue
    return CcxtVenue(x, profile)


def stop_supported(profile: Profile, has: dict) -> bool:
    """Whether the relay can rest a stop order on this exchange."""
    return profile.capabilities.native_stop and any(has.get(flag) for flag in STOP_FLAGS)


def spot_stop_supported(spot_features: dict) -> bool:
    """Whether CCXT can rest a stop-market sell on this exchange's spot market (its features
    table: createOrder.stopLossPrice). Unknown counts as no."""
    return bool(((spot_features or {}).get("createOrder") or {}).get("stopLossPrice"))


def _build_plugin(name: str, environment: str, *, paper: bool, environ, market: str):
    """A venue from an installed venue plugin (relay.plugins.VenuePlugin)."""
    from relay.plugins import VENUE_CALLS, PluginError, load
    try:
        factory = load("venue", name)
    except PluginError as e:
        raise VenueConfigError(f"{name!r} is not an exchange CCXT knows, and {e}") from None
    if market != "perpetual":
        raise VenueConfigError(f"{name} is a venue plugin: it trades its own markets, leave venue.market empty")
    try:
        client = factory(environment, paper=paper, environ=environ)
    except Exception as e:
        raise VenueConfigError(f"the venue plugin {name} could not be built: {type(e).__name__}: {e}") from e
    missing = [call for call in VENUE_CALLS if not callable(getattr(client, call, None))]
    if missing:
        raise VenueConfigError(f"the venue plugin {name} lacks the call(s) {', '.join(missing)} (docs/plugins/venues.md)")
    if getattr(client, "venue_id", None) != name:
        # Positions and the account are recorded under the deployment's exchange name, and every
        # account check matches the client's venue_id: the two must be one name.
        raise VenueConfigError(f"the venue plugin {name} reports venue_id {getattr(client, 'venue_id', None)!r}: "
                               f"it must be {name!r}, the name it is registered under")
    for flag in ("can_stop", "can_short"):
        if not isinstance(getattr(client, flag, None), bool):
            raise VenueConfigError(f"the venue plugin {name} must say {flag} = True or False")
    client.plugin_name = name
    return client


def client_problems(client, settings: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(refusals, warnings) for this venue client: a CCXT connector by its profile, a plugin by
    what it says it can do."""
    profile = getattr(client, "profile", None)
    if profile is not None:
        return venue_problems(profile, client.x.has, settings, spot_features=getattr(client, "spot_features", None))
    name = getattr(client, "plugin_name", client.venue_id)
    refuse, warn = [], []
    _stop_problem(name, client.can_stop is True, settings, refuse, warn)
    # What a plugin does not say it can do, it cannot (unlike CCXT, there is no table to ask).
    for key, flag, what in (("trailing_distance_pct", "can_trail", "a trailing stop"),
                            ("take_profit_ladder", "can_ladder", "resting reduce-only limit orders"),
                            ("leverage", "can_set_leverage", "setting leverage"),
                            ("isolated_margin", "can_set_margin_mode", "setting the margin mode")):
        if settings.get(key) not in (None, [], 0) and getattr(client, flag, False) is not True:
            refuse.append(f"{key} is set, but the venue plugin {name} does not offer {what} ({flag}): leave it empty")
    if settings.get("daily_loss_limit_pct") and getattr(client, "gives_pnl", False) is not True:
        warn.append(f"daily_loss_limit_pct is set, but the venue plugin {name} gives no realised PnL (gives_pnl): a "
                    f"close the exchange makes stays unpriced, and new entries are refused that day until it is")
    warn.append(f"{name} is a venue plugin: run relay verify-venue on its test account before trusting it")
    return refuse, warn


async def close_client(client) -> None:
    """Release a venue client's connections (a CCXT exchange, or a plugin's close())."""
    close = getattr(getattr(client, "x", None), "close", None) or getattr(client, "close", None)
    if close is not None:
        await close()


def venue_problems(profile: Profile, has: dict, settings: dict[str, Any], *,
                   spot_features: dict | None = None) -> tuple[list[str], list[str]]:
    """(refusals, warnings) for running these deployment settings on this exchange (on its spot
    market when spot_features is given). A refusal stops the start: the relay would otherwise
    accept a setting the exchange cannot carry out."""
    if spot_features is not None:
        return _spot_problems(profile, spot_stop_supported(spot_features), settings)
    caps = profile.capabilities
    refuse, warn = [], []
    name = profile.exchange
    _stop_problem(name, stop_supported(profile, has), settings, refuse, warn)
    if settings.get("trailing_distance_pct") and not (caps.trailing_stop and has.get("createTrailingPercentOrder")):
        refuse.append(f"position_management.trailing_distance_pct is set, but {name} has no trailing stop "
                      f"through the relay: leave it empty")
    if settings.get("take_profit_ladder") and not caps.reduce_only_limit:
        refuse.append(f"position_management.take_profit_ladder is set, but {name} cannot rest reduce-only "
                      f"limit orders: leave it empty")
    if not stop_supported(profile, has):
        for key in ("take_profit_pct", "break_even_trigger_pct", "break_even_trigger_r", "break_even_trigger_tp"):
            if settings.get(key):
                refuse.append(f"position_management.{key} is set, but {name} cannot hold a stop or take-profit "
                              f"order: leave it empty")
    if settings.get("isolated_margin") is not None and not (has.get("setMarginMode") and caps.set_margin_mode):
        refuse.append(f"isolated_margin is set, but {name} cannot change the margin mode through its API: "
                      f"leave it empty and set the mode on the exchange")
    if settings.get("leverage") and not (has.get("setLeverage") and caps.set_leverage):
        refuse.append(f"leverage is set, but {name} cannot change leverage through its API: leave it empty")
    if settings.get("daily_loss_limit_pct") and not caps.closed_pnl:
        warn.append(f"daily_loss_limit_pct is set, but {name} publishes no realised PnL per fill: a close "
                    f"the exchange makes (a stop that fires) stays unpriced, and new entries are refused "
                    f"that day until it is")
    if not profile.verified:
        warn.append(f"{name} is a community connector: not yet run against the exchange's test account "
                    f"(docs/venues.md says how to verify it)")
    return refuse, warn


def _stop_problem(name: str, can_stop: bool, settings: dict[str, Any], refuse: list, warn: list) -> None:
    if can_stop:
        return
    if settings.get("require_stop", True):
        refuse.append(f"{name} cannot hold a stop order through its API, and require_stop is on. To trade it "
                      f"without a stop on the exchange, set require_stop: false and send signals without a stop")
    else:
        warn.append(f"{name} cannot hold a stop order through its API: positions there have no stop on the "
                    f"exchange, and a signal that asks for a stop is refused")


def _spot_problems(profile: Profile, can_stop: bool, settings: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Spot: long only, no leverage, and the stop holds the coins - so nothing else may rest on
    them (no take-profit order, no ladder, no trailing stop)."""
    name = f"{profile.exchange} spot"
    refuse, warn = [], []
    if not profile.spot_supported:
        return [f"{name} is not offered: {profile.notes}"], warn
    _stop_problem(name, can_stop, settings, refuse, warn)
    for key, why in (("leverage", "spot has no leverage"),
                     ("isolated_margin", "spot has no margin mode"),
                     ("take_profit_ladder", "the stop holds the coins, so no ladder level can rest beside it"),
                     ("take_profit_pct", "the stop holds the coins, so no take-profit order can rest beside it"),
                     ("trailing_distance_pct", "the relay offers no trailing stop on spot")):
        if settings.get(key) not in (None, [], 0):
            refuse.append(f"{key} is set, but {why}: leave it empty")
    warn.append(f"{name}: long only - a short signal, a flip to short and a book with short legs are refused")
    warn.append(f"{name}: a take-profit sent with a signal is not placed (the stop holds the coins)")
    if settings.get("daily_loss_limit_pct"):
        warn.append(f"daily_loss_limit_pct is set, but {name} publishes no realised PnL: a close the exchange "
                    f"makes (a stop that fires) stays unpriced, and new entries are refused that day until it is")
    if not profile.verified:
        warn.append(f"{name} is a community connector: not yet run against the exchange's test account "
                    f"(docs/venues.md says how to verify it)")
    return refuse, warn
