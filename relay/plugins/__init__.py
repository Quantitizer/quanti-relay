"""Plugins: sources, sinks and venues from other Python packages, without editing the relay.

A plugin is installed with `pip install` and registers itself under a package entry point. The
relay loads one only when a config file names it, never on its own:

    # the plugin package's pyproject.toml
    [project.entry-points."quanti_relay.sinks"]
    pager = "relay_pager:PagerSink"

    [project.entry-points."quanti_relay.sources"]
    acme = "relay_acme:AcmeSource"

    [project.entry-points."quanti_relay.venues"]
    myexchange = "relay_myexchange:build"

Then `type: pager` in a sink file, `format: acme` in a source file, or `exchange: myexchange` in
the deployment's venue section uses it. A built-in name (telegram, hmac, bybit, a CCXT exchange id)
always means the built-in one: a plugin cannot replace what is shipped.

The three kinds (docs/plugins/ has a guide for each; relay/plugins/_template_*.py is a working
starting point; relay.plugins.contract has the checks a plugin must pass):

- SourcePlugin translates a sender's own JSON into the relay's v1 signal. It does not prove who
  sent it: that stays with the relay (the source's hmac signature or passphrase), so a plugin can
  never weaken it.
- SinkPlugin delivers the relay's events somewhere (a pager, a chat, a log service).
- VenuePlugin builds a venue client: the calls the relay makes on an exchange, answered in the
  shape docs/venues.md lists ("The calls"). `relay verify-venue` checks one on a test account.
"""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

GROUPS = {"source": "quanti_relay.sources", "sink": "quanti_relay.sinks", "venue": "quanti_relay.venues"}


class PluginError(ValueError):
    """A plugin the relay will not run with (the message says which and why)."""


@runtime_checkable
class SourcePlugin(Protocol):
    """Turns a sender's payload into a v1 signal body (schemas/signal.v1.json).

    `translate` gets the JSON object the sender posted (after the relay has checked who sent it,
    and with a passphrase already removed) and returns the v1 body. It raises ValueError for a
    payload it cannot read; the relay answers 422 with the message. It must not do I/O: it is a
    pure translation, called once per request."""
    name: str

    def translate(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class SinkPlugin(Protocol):
    """Delivers the relay's events. Built from a sink file by `from_config`.

    `from_config(name, settings, environ)`: `settings` is the sink file without the common keys
    (type, enabled, min_severity, kinds). Secrets come from environment variables the file names
    in keys ending in `_env` (a file holding a secret is refused by the relay before the plugin
    sees it). Raise ValueError for settings it cannot use.

    `send(http, event)` delivers one event ({"id", "kind", "severity", "message", "data",
    "created_at", ...}) and raises relay.sinks.SendError on any failure: refused=True when the
    receiver refuses that event outright (it is skipped after a few refusals), else it is retried
    later; retry_after when the receiver says how long to wait. `redact(text)` returns text without
    the sink's secrets (it goes into the relay's log)."""
    name: str

    async def send(self, http: Any, event: dict[str, Any]) -> None: ...

    def redact(self, text: str) -> str: ...


@runtime_checkable
class VenuePlugin(Protocol):
    """Builds the venue client for a deployment: `build(environment, *, paper, environ)`, where
    environment is demo, testnet or live (the deployment's venue.environment), paper is whether
    the deployment is on dry-run (then no keys may be needed), and environ holds the keys.

    The client answers the calls in docs/venues.md ("The calls") and has `venue_id` (lowercase
    letters and digits: what its positions are recorded under), `can_stop` (whether a stop can rest
    on the venue), `can_short`, and `async prepare() -> list[str]` (warnings at start)."""

    def __call__(self, environment: str, *, paper: bool, environ: Any) -> Any: ...


VENUE_CALLS = ("list_instruments", "get_ticker", "get_balance_safe", "get_positions", "get_open_orders",
               "get_order", "get_order_history", "get_closed_pnl", "get_transaction_log", "place_market_order",
               "place_limit_order", "amend_order", "cancel_order", "cancel_all_orders", "set_trading_stop",
               "set_leverage", "switch_isolated_margin", "prepare")


def load(kind: str, name: str) -> Any:
    """The plugin of this kind registered under this name. Raises PluginError when none is (or it
    cannot be imported)."""
    group = GROUPS[kind]
    found = [ep for ep in entry_points(group=group) if ep.name == name]
    if not found:
        raise PluginError(f"no {kind} plugin named {name!r} is installed (entry point group {group}; "
                          f"docs/plugins/ says how to add one)")
    if len(found) > 1:
        raise PluginError(f"two installed packages register a {kind} plugin named {name!r}: remove one")
    try:
        return found[0].load()
    except Exception as e:
        raise PluginError(f"the {kind} plugin {name!r} could not be loaded: {type(e).__name__}: {e}") from e


def installed(kind: str) -> list[str]:
    """The names of the installed plugins of this kind."""
    return sorted(ep.name for ep in entry_points(group=GROUPS[kind]))
