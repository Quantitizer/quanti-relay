"""Sources: who may send signals, how they prove it, and which deployments they may reach.

Each source is one YAML file in `<config_dir>/sources/` (docs/SPEC.md §4):

    # config/sources/mybot.yaml
    name: mybot                       # optional; defaults to the file name
    auth: hmac
    secret_env: RELAY_SOURCE_MYBOT_SECRET
    deployments: [default]
    enabled: true                     # optional; false = refuse everything from it
    max_signals_per_minute:           # optional; unset = no limit
    actions: [open, close]            # optional; what it may send (unset = everything): open, close,
                                      # reduce, flip, close_all, move_stop, target (a whole book)

The secret itself is never in the file: it is read from the environment variable named by
`secret_env` (a file that holds a `secret` is refused). It must be at least 32 characters.

HMAC-SHA256 signing (as Stripe does it): the sender puts
    X-Relay-Signature: t=<unix seconds>,v1=<hex hmac>
on the request, where the hmac is over `<t>.<raw body bytes>` with the source's secret. A signature
older or newer than SIGNATURE_WINDOW_S is refused (a captured request cannot be replayed later; a
replay inside the window is caught by the signal id, which is never executed twice).

Give every relay its own secret: the signature does not name the relay, so a request signed for
one relay is valid at another that shares the secret (and has a deployment of the same name).

Passphrase (for TradingView, which cannot sign a request):

    # config/sources/tradingview.yaml
    auth: passphrase
    secret_env: RELAY_TRADINGVIEW_PASSPHRASE   # the passphrase (at least 32 characters)
    allow_ips: []                     # REQUIRED: the addresses TradingView sends webhooks from,
                                      # as TradingView publishes them (IPs or CIDR ranges)
    client_ip_header:                 # behind a reverse proxy: the header it puts the client's
                                      # address in (e.g. X-Forwarded-For); empty = the direct peer
    trusted_proxies: []               # with client_ip_header, REQUIRED: your proxy's address(es)
    deployments: [main]

The alert's JSON carries `"passphrase": "..."` next to the v1 fields; it is compared in constant
time and removed before the signal is read or stored. A request from an address not on the list
is refused whatever it carries.

Behind a proxy, the header is read only on requests that come from one of trusted_proxies (a
request reaching the relay any other way is refused), and it is read from the right: proxies
append the address they saw, so the entries on the left are whatever the client chose to send.
The client's address is the right-most entry that is not itself a trusted proxy.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from relay.deployment_config import read_yaml

SIGNATURE_HEADER = "X-Relay-Signature"
SIGNATURE_WINDOW_S = 30
MIN_SECRET_LENGTH = 32
_FIELDS = {"name", "auth", "secret_env", "deployments", "enabled", "max_signals_per_minute", "allow_ips",
           "client_ip_header", "trusted_proxies", "format", "actions"}
# What a source may be allowed to send: the event actions, and `target` for a whole book.
ACTIONS = ("open", "close", "reduce", "flip", "close_all", "move_stop", "target")
# Names the relay uses for its own ways in (POST /v1/orders, the console's test signal).
RESERVED_NAMES = ("api", "console")
PASSPHRASE_FIELD = "passphrase"
_HEX = set("0123456789abcdef")


class SourceConfigError(ValueError):
    """A source file the relay will not run with (the message says which and why)."""


@dataclass(frozen=True)
class Source:
    name: str
    auth: str
    secret_env: str
    deployments: tuple[str, ...]
    enabled: bool = True
    max_signals_per_minute: int | None = None
    allow_ips: tuple = ()                      # passphrase sources: ip_network objects
    client_ip_header: str | None = None
    trusted_proxies: tuple = ()                # with client_ip_header: ip_network objects
    format: str | None = None                  # a source plugin that translates its payload to v1
    actions: tuple[str, ...] = ()              # what it may send; empty = everything
    _secret: str = field(default="", repr=False)
    _translator: Any = field(default=None, repr=False, compare=False)
    _sent: deque = field(default_factory=deque, repr=False, compare=False)

    def translate(self, payload: dict) -> dict:
        """The payload as a v1 signal body: through the source's plugin when it names a format,
        else as it came. Raises ValueError for one the plugin cannot read (or that it turns into
        something that is not a JSON object: TypeError)."""
        if self._translator is None:
            return payload
        out = self._translator.translate(dict(payload))
        if not isinstance(out, dict):
            raise TypeError(f"the {self.format} plugin returned {type(out).__name__}, not a signal object")
        out = {k: v for k, v in out.items() if k != PASSPHRASE_FIELD}
        out["source"] = self.name                  # the source that was checked, whatever the plugin says
        return json.loads(json.dumps(out))         # plain JSON, as stored (a datetime would fail the log)

    def over_rate(self, now: float | None = None) -> bool:
        """True if one more signal now would pass max_signals_per_minute (counted over the last
        60 s); otherwise it is counted. Only signals with a valid signature are counted, so nobody
        without the secret can use up the source's allowance."""
        if self.max_signals_per_minute is None:
            return False
        now = time.monotonic() if now is None else now
        while self._sent and self._sent[0] <= now - 60:
            self._sent.popleft()
        if len(self._sent) >= self.max_signals_per_minute:
            return True
        self._sent.append(now)
        return False

    def may_send(self, signal) -> bool:
        """Whether this source may send this signal's action (a target book counts as `target`)."""
        what = "target" if getattr(signal, "kind", None) == "target" else getattr(signal, "action", None)
        return not self.actions or what in self.actions

    def may_reach(self, deployment: str) -> bool:
        return deployment in self.deployments

    def sign(self, body: bytes, t: int | None = None) -> str:
        """The signature header for `body` with this source's secret (relay test-signal)."""
        return sign(self._secret, body, t)

    def verify(self, header: str | None, body: bytes, now: float | None = None) -> str | None:
        """Why this request's signature is not accepted, or None when it is."""
        return verify_signature(self._secret, header, body, now)

    def client_address(self, peer: str | None, header: str | None) -> str | None:
        """The sender's address: the direct peer, or behind trusted proxies the right-most entry
        of client_ip_header that is not one of them (None = cannot be told)."""
        if not self.client_ip_header:
            return peer
        if not _in(peer, self.trusted_proxies):
            return None                        # not through the proxy: the header could be anyone's
        for entry in reversed((header or "").split(",")):
            if not _in(entry, self.trusted_proxies):
                return entry.strip() or None
        return None

    def verify_passphrase(self, given: object, client_ip: str | None) -> str | None:
        """Why a passphrase request is not accepted (address first, then passphrase), or None."""
        try:
            addr = ipaddress.ip_address((client_ip or "").strip())
        except ValueError:
            return f"client address {client_ip!r} unreadable"
        if not any(addr in net for net in self.allow_ips):
            return f"address {addr} is not on the source's allow_ips"
        # surrogatepass: a lone surrogate (valid JSON) must not raise with the secret in scope.
        if not isinstance(given, str) or not hmac.compare_digest(given.encode("utf-8", "surrogatepass"),
                                                                 self._secret.encode()):
            return "passphrase does not match"
        return None


def _in(address: str | None, networks: tuple) -> bool:
    try:
        addr = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _networks(path: Path, key: str, listed) -> tuple:
    try:
        return tuple(ipaddress.ip_network(str(x).strip(), strict=False) for x in listed)
    except ValueError as e:
        raise SourceConfigError(f"{path}: {key}: {e}") from None


def sign(secret: str, body: bytes, t: int | None = None) -> str:
    """The X-Relay-Signature header value for `body` (for senders, tests and `relay test-signal`)."""
    t = int(time.time()) if t is None else int(t)
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_signature(secret: str, header: str | None, body: bytes, now: float | None = None) -> str | None:
    """Why `header` does not sign `body` with `secret` inside the time window, or None if it does."""
    if not secret:
        return "the source has no secret"
    if not header:
        return f"missing {SIGNATURE_HEADER} header"
    t, macs = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        value = value.strip()
        if key == "t" and value.isascii() and value.isdecimal() and len(value) <= 12:
            t = int(value)
        elif key == "v1" and value.isascii() and value and all(c in _HEX for c in value.lower()):
            macs.append(value.lower())
    if t is None or not macs:
        return f"malformed {SIGNATURE_HEADER} header (expected t=<unix>,v1=<hex>)"
    now = time.time() if now is None else now
    if abs(now - t) > SIGNATURE_WINDOW_S:
        return f"signature timestamp outside the {SIGNATURE_WINDOW_S} s window (check the sender's clock)"
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    # Several v1 values may be sent (e.g. while a secret is being rotated); one must match.
    if not any(hmac.compare_digest(expected, mac) for mac in macs):
        return "signature does not match"
    return None


def _load_one(path: Path, environ) -> Source:
    try:
        raw = read_yaml(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SourceConfigError(f"{path}: not valid YAML ({e})") from e
    if not isinstance(raw, dict):
        raise SourceConfigError(f"{path}: expected a mapping of settings")
    if "secret" in raw:
        raise SourceConfigError(f"{path}: a secret never goes in a config file - put it in an environment "
                                f"variable and name that variable in secret_env")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise SourceConfigError(f"{path}: unknown setting(s) {sorted(unknown)}")
    name = raw.get("name", path.stem)
    if not isinstance(name, str) or not name or len(name) > 50:
        raise SourceConfigError(f"{path}: name must be text of 1-50 characters")
    if name in RESERVED_NAMES:
        raise SourceConfigError(f"{path}: the name {name!r} is the relay's own: choose another")
    auth = raw.get("auth")
    if auth not in ("hmac", "passphrase"):
        raise SourceConfigError(f"{path}: auth must be hmac or passphrase")
    allow_ips: tuple = ()
    if auth == "passphrase":
        listed = raw.get("allow_ips")
        if not isinstance(listed, list) or not listed:
            raise SourceConfigError(f"{path}: a passphrase source needs allow_ips: the addresses it sends from "
                                    f"(for TradingView: the ones TradingView publishes for its webhooks)")
        allow_ips = _networks(path, "allow_ips", listed)
    elif raw.get("allow_ips") is not None:
        raise SourceConfigError(f"{path}: allow_ips belongs to a passphrase source")
    ip_header = raw.get("client_ip_header")
    if ip_header is not None and (auth != "passphrase" or not isinstance(ip_header, str) or not ip_header.strip()):
        raise SourceConfigError(f"{path}: client_ip_header is the header name a proxy puts the client's address "
                                f"in, for a passphrase source")
    proxies = raw.get("trusted_proxies")
    trusted: tuple = ()
    if ip_header is not None:
        if not isinstance(proxies, list) or not proxies:
            raise SourceConfigError(f"{path}: with client_ip_header, trusted_proxies must list your proxy's "
                                    f"address(es): the header is believed only on requests from them")
        trusted = _networks(path, "trusted_proxies", proxies)
    elif proxies is not None and proxies != []:
        raise SourceConfigError(f"{path}: trusted_proxies belongs with client_ip_header")
    secret_env = raw.get("secret_env")
    if not isinstance(secret_env, str) or not secret_env:
        raise SourceConfigError(f"{path}: secret_env must name the environment variable holding the secret")
    deployments = raw.get("deployments")
    if not isinstance(deployments, list) or not deployments or not all(isinstance(d, str) and d for d in deployments):
        raise SourceConfigError(f"{path}: deployments must list the deployment name(s) this source may reach")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SourceConfigError(f"{path}: enabled must be true or false")
    rate = raw.get("max_signals_per_minute")
    if rate is not None and (isinstance(rate, bool) or not isinstance(rate, int) or rate < 1):
        raise SourceConfigError(f"{path}: max_signals_per_minute must be a whole number of at least 1 (or unset)")
    actions = raw.get("actions")
    if actions is not None and (not isinstance(actions, list) or not actions
                                or any(a not in ACTIONS for a in actions)):
        raise SourceConfigError(f"{path}: actions must list what the source may send, from {list(ACTIONS)} "
                                f"(or leave it out for everything)")
    fmt = raw.get("format")
    translator = None
    if fmt is not None:
        from relay.plugins import PluginError, load
        if not isinstance(fmt, str) or not fmt:
            raise SourceConfigError(f"{path}: format names an installed source plugin (or leave it out for v1)")
        try:
            translator = load("source", fmt)
        except PluginError as e:
            raise SourceConfigError(f"{path}: {e}") from e
        if isinstance(translator, type):
            translator = translator()
        if not callable(getattr(translator, "translate", None)):
            raise SourceConfigError(f"{path}: the source plugin {fmt!r} has no translate(payload)")
    secret = environ.get(secret_env, "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise SourceConfigError(f"{path}: the environment variable {secret_env} must hold a secret of at least "
                                f"{MIN_SECRET_LENGTH} characters" + (" (it is not set: with Docker, set it in "
                                                                     ".env; the relay itself does not read .env)"
                                                                     if not secret else ""))
    return Source(name=name, auth=auth, secret_env=secret_env, deployments=tuple(deployments),
                  enabled=enabled, max_signals_per_minute=rate, allow_ips=allow_ips,
                  client_ip_header=ip_header.strip() if ip_header else None, trusted_proxies=trusted,
                  format=fmt, actions=tuple(actions or ()), _secret=secret, _translator=translator)


def load_sources(config_dir: str | Path, environ=None) -> dict[str, Source]:
    """Every source in `<config_dir>/sources/*.yaml`, by name. No directory = no sources (signals
    are then refused). Raises SourceConfigError for a file it will not run with: a bad source must
    stop the relay at start, not be skipped."""
    environ = os.environ if environ is None else environ
    folder = Path(config_dir) / "sources"
    sources: dict[str, Source] = {}
    if not folder.is_dir():
        return sources
    for path in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]):
        src = _load_one(path, environ)
        if src.name in sources:
            raise SourceConfigError(f"{path}: a second source named {src.name!r}")
        sources[src.name] = src
    logger.info(f"[Sources] {len(sources)} source(s): "
                + (", ".join(f"{s.name} -> {list(s.deployments)}{'' if s.enabled else ' (disabled)'}"
                             for s in sources.values()) or "none"))
    return sources
