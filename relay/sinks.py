"""Notification sinks: the relay's events (relay.events) delivered to Telegram, Discord or any
webhook. Notifications only: nothing a sink receives can control the relay.

Each sink is one YAML file in `<config_dir>/sinks/`:

    # config/sinks/phone.yaml
    type: telegram                    # telegram | discord | webhook
    enabled: true
    min_severity: warning             # info | warning | critical (default info: everything)
    kinds: []                         # only these kinds or prefixes, e.g. [position., relay.halted]; [] = all
    # telegram:  bot_token_env: RELAY_TELEGRAM_BOT_TOKEN   chat_id_env: RELAY_TELEGRAM_CHAT_ID
    # discord:   webhook_url_env: RELAY_DISCORD_WEBHOOK_URL
    # webhook:   url_env: RELAY_EVENTS_WEBHOOK_URL         secret_env: (optional) RELAY_EVENTS_WEBHOOK_SECRET

Tokens, URLs and secrets come from environment variables only (a file holding one is refused). A
webhook with secret_env is signed like an incoming signal: X-Relay-Signature: t=<unix>,v1=<hmac of
"<t>.<body>">, so the receiver can check it came from this relay.

Delivery (deliver_once, every notify_interval_s): each sink keeps its own place in the event list
(cursor "sink:<name>", starting at the events that exist when it is first set up - no history
flood). Sinks are served side by side, so one that is slow or down holds back no other.

A failed send is retried, never dropped, while the receiver is down or busy (no answer, 5xx, 408,
429): the sink waits before its next try (its Retry-After when it gives one, else doubling up to
BACKOFF_MAX_S). Only an event the receiver refuses outright (another 4xx) is skipped, after
MAX_ATTEMPTS refusals, so one bad event cannot block the sink forever.

Event ids are handed out before their transaction commits, so on Postgres a later id can be seen
first. A sink stops at a gap in the ids until it is GAP_WAIT_S old (a gap that stays is a
rolled-back insert), so a late event is not passed over.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from loguru import logger
from sqlalchemy import func, select

from relay.db.models import Event
from relay.deployment_config import read_yaml
from relay.events import SEVERITIES
from relay.signals.sources import SIGNATURE_HEADER, sign

MAX_ATTEMPTS = 5            # outright refusals of one event before it is skipped
BATCH = 50
BACKOFF_MAX_S = 300         # longest wait between tries at a sink that is down
ROUND_BUDGET_S = 30         # a sink stops its round after this long (the rest goes next round)
GAP_WAIT_S = 30
PLUGIN_SEND_TIMEOUT_S = 20  # a sink plugin's send that takes longer is a failure (retried later)
# Setting names that hold a secret: in a plugin sink file only as <name>_env (an environment variable).
_SECRET_WORDS = ("token", "secret", "password", "passphrase", "key", "url", "webhook", "dsn", "auth", "credential",
                 "endpoint", "cookie")
_COMMON = {"type", "enabled", "min_severity", "kinds"}
_ENV_FIELDS = {"telegram": ("bot_token_env", "chat_id_env"), "discord": ("webhook_url_env",),
               "webhook": ("url_env",)}
_OPTIONAL = {"webhook": ("secret_env",)}
_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


class SinkConfigError(ValueError):
    """A sink file the relay will not run with (the message says which and why)."""


class SendError(RuntimeError):
    """A send that did not go through. `refused`: the receiver turned this event down (retrying it
    will not help); otherwise it is down or busy. `retry_after`: seconds it asked us to wait."""

    def __init__(self, message: str, refused: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.refused, self.retry_after = refused, retry_after


def _retry_after(r: httpx.Response) -> float | None:
    """Telegram: parameters.retry_after; Discord: retry_after; anyone: the Retry-After header."""
    try:
        return float(r.headers["Retry-After"])
    except (KeyError, ValueError):
        pass
    try:
        body = r.json()
        value = body.get("retry_after", (body.get("parameters") or {}).get("retry_after"))
        return None if value is None else float(value)
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass
class Sink:
    name: str
    type: str
    min_severity: str = "info"
    kinds: tuple[str, ...] = ()
    _env: dict[str, str] = field(default_factory=dict, repr=False)      # resolved secrets / URLs

    def wants(self, kind: str, severity: str) -> bool:
        if SEVERITIES.index(severity) < SEVERITIES.index(self.min_severity):
            return False
        return not self.kinds or any(kind == k or (k.endswith(".") and kind.startswith(k)) for k in self.kinds)

    def redact(self, text: str) -> str:
        """`text` without this sink's tokens, URLs and secrets (for log lines)."""
        for value in self._env.values():
            if value:
                text = text.replace(value, "***")
        return text

    async def send(self, http: httpx.AsyncClient, event: dict[str, Any]) -> None:
        """Deliver one event; raises SendError on any failure (the delivery loop retries)."""
        text = f"{_ICON.get(event['severity'], '')} [{event['severity']}] {event['kind']}\n{event['message']}"
        if self.type == "telegram":
            r = await http.post(f"https://api.telegram.org/bot{self._env['bot_token_env']}/sendMessage",
                                json={"chat_id": self._env['chat_id_env'], "text": text[:4000],
                                      "disable_web_page_preview": True})
        elif self.type == "discord":
            r = await http.post(self._env['webhook_url_env'], json={"content": text[:1900]})
        else:
            body = json.dumps(event, sort_keys=True, default=str).encode()
            headers = {"Content-Type": "application/json"}
            if self._env.get('secret_env'):
                headers[SIGNATURE_HEADER] = sign(self._env['secret_env'], body)
            r = await http.post(self._env['url_env'], content=body, headers=headers)
        if r.status_code >= 300:
            # Never echo a token-bearing URL; the status and a little of the answer are enough.
            # A redirect is refused too: it is not followed (the event would go somewhere else), and
            # retrying it would hold this sink up forever. The file needs the final URL.
            refused = r.status_code < 500 and r.status_code not in (408, 429)
            hint = " (a redirect: put the final URL in the variable)" if r.status_code < 400 else ""
            raise SendError(f"{self.type} answered {r.status_code}{hint}: {r.text[:200]}", refused=refused,
                            retry_after=_retry_after(r) if r.status_code == 429 else None)


def _load_one(path: Path, environ) -> Sink | None:
    try:
        raw = read_yaml(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SinkConfigError(f"{path}: not valid YAML ({e})") from e
    if not isinstance(raw, dict):
        raise SinkConfigError(f"{path}: expected a mapping of settings")
    kind = raw.get("type")
    if not isinstance(kind, str) or not kind:
        raise SinkConfigError(f"{path}: type must be one of {sorted(_ENV_FIELDS)} or an installed sink plugin")
    if kind not in _ENV_FIELDS:
        return _load_plugin(path, raw, kind, environ)
    allowed = _COMMON | set(_ENV_FIELDS[kind]) | set(_OPTIONAL.get(kind, ()))
    secretish = {k for k in raw if k in ("token", "bot_token", "url", "webhook_url", "secret", "chat_id")}
    if secretish:
        raise SinkConfigError(f"{path}: {sorted(secretish)} never go in a config file - put them in environment "
                              f"variables and name those in {sorted(set(_ENV_FIELDS[kind]))}")
    unknown = set(raw) - allowed
    if unknown:
        raise SinkConfigError(f"{path}: unknown setting(s) {sorted(unknown)} for a {kind} sink")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SinkConfigError(f"{path}: enabled must be true or false")
    severity = raw.get("min_severity", "info")
    if severity not in SEVERITIES:
        raise SinkConfigError(f"{path}: min_severity must be one of {list(SEVERITIES)}")
    kinds = raw.get("kinds") or []
    if not isinstance(kinds, list) or not all(isinstance(k, str) and k for k in kinds):
        raise SinkConfigError(f"{path}: kinds must list event kinds or prefixes ending in '.'")
    if not enabled:
        return None                     # switched off: its variables need not be set
    env: dict[str, str] = {}
    for key in _ENV_FIELDS[kind] + _OPTIONAL.get(kind, ()):
        var = raw.get(key)
        if var is None and key in _OPTIONAL.get(kind, ()):
            continue
        if not isinstance(var, str) or not var:
            raise SinkConfigError(f"{path}: {key} must name the environment variable that holds it")
        value = environ.get(var, "")
        if not value:
            raise SinkConfigError(f"{path}: the environment variable {var} ({key}) is not set (set it in the "
                                  f"relay's environment: with Docker, in .env)")
        env[key] = value
    for key in ("webhook_url_env", "url_env"):
        if key in env and not env[key].startswith("https://"):
            raise SinkConfigError(f"{path}: the URL in {raw[key]} must start with https:// (events are private)")
    return Sink(name=path.stem, type=kind, min_severity=severity, kinds=tuple(kinds), _env=env)


def _common(path: Path, raw: dict) -> tuple[bool, str, tuple[str, ...]]:
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SinkConfigError(f"{path}: enabled must be true or false")
    severity = raw.get("min_severity", "info")
    if severity not in SEVERITIES:
        raise SinkConfigError(f"{path}: min_severity must be one of {list(SEVERITIES)}")
    kinds = raw.get("kinds") or []
    if not isinstance(kinds, list) or not all(isinstance(k, str) and k for k in kinds):
        raise SinkConfigError(f"{path}: kinds must list event kinds or prefixes ending in '.'")
    return enabled, severity, tuple(kinds)


@dataclass
class PluginSink(Sink):
    """A sink from an installed plugin (relay.plugins.SinkPlugin). The relay still decides which
    events it gets and keeps its secrets out of the log."""
    plugin: Any = None

    async def send(self, http: httpx.AsyncClient, event: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(self.plugin.send(http, event), timeout=PLUGIN_SEND_TIMEOUT_S)
        except TimeoutError:
            raise SendError(f"the {self.type} plugin did not finish within {PLUGIN_SEND_TIMEOUT_S} s") from None
        except SendError:
            raise
        except Exception as e:                       # anything else the plugin raises: retried later
            raise SendError(f"the {self.type} plugin failed: {type(e).__name__}: {e}") from e

    def redact(self, text: str) -> str:
        try:
            hidden = self.plugin.redact(text)
        except Exception:
            hidden = None
        return super().redact(hidden if isinstance(hidden, str) else text)


def _load_plugin(path: Path, raw: dict, kind: str, environ) -> Sink | None:
    from relay.plugins import PluginError, load
    secretish = {k for k in raw if not k.endswith("_env") and any(w in k.lower() for w in _SECRET_WORDS)}
    if secretish:
        raise SinkConfigError(f"{path}: {sorted(secretish)} never go in a config file - put them in environment "
                              f"variables and name those in settings ending in _env")
    enabled, severity, kinds = _common(path, raw)
    if not enabled:
        return None                                  # a sink that is off is not loaded at all
    env: dict[str, str] = {}
    named: dict[str, str] = {}
    for key, var in raw.items():
        if key.endswith("_env"):
            if not isinstance(var, str) or not environ.get(var):
                raise SinkConfigError(f"{path}: {key} must name a set environment variable (with "
                                      f"Docker, set it in .env; the relay itself does not read .env)")
            env[key] = environ[var]
            named[var] = environ[var]
    try:
        plugin_cls = load("sink", kind)
        settings = {k: v for k, v in raw.items() if k not in _COMMON}
        # Only the variables the file names: the relay's own keys are none of a sink's business.
        plugin = plugin_cls.from_config(path.stem, settings, named)
    except PluginError as e:
        raise SinkConfigError(f"{path}: type must be one of {sorted(_ENV_FIELDS)} or an installed sink plugin "
                              f"({e})") from e
    except (ValueError, TypeError, KeyError) as e:
        raise SinkConfigError(f"{path}: the {kind} sink plugin refused its settings: {e}") from e
    return PluginSink(name=path.stem, type=kind, min_severity=severity, kinds=kinds, _env=env, plugin=plugin)


def load_sinks(config_dir: str | Path, environ=None) -> list[Sink]:
    """The enabled sinks in `<config_dir>/sinks/*.yaml`. A file the relay cannot use stops the start."""
    environ = os.environ if environ is None else environ
    folder = Path(config_dir) / "sinks"
    if not folder.is_dir():
        return []
    sinks = [s for s in (_load_one(p, environ) for p in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]))
             if s is not None]
    logger.info(f"[Sinks] {len(sinks)} sink(s): "
                + (", ".join(f"{s.name} ({s.type}, {s.min_severity}+)" for s in sinks) or "none"))
    return sinks


class Notifier:
    """Delivers the event log to the sinks."""

    def __init__(self, events, sinks: list[Sink], http: httpx.AsyncClient | None = None):
        self.events = events
        self.sinks = sinks
        self.http = http or httpx.AsyncClient(timeout=10)
        self.clock = time.monotonic
        self._refusals: dict[tuple[str, int], int] = {}
        self._failures: dict[str, int] = {}            # sink -> sends failed in a row (down / busy)
        self._not_before: dict[str, float] = {}         # sink -> clock time of its next try
        self._round = asyncio.Lock()          # one round at a time: never the same event twice

    async def start_cursors(self) -> None:
        """A sink seen for the first time starts after the events that already exist."""
        async with self.events.db.session() as s:
            last = (await s.execute(select(func.max(Event.id)))).scalar() or 0
        for sink in self.sinks:
            if await self.events.cursor(f"sink:{sink.name}") is None:
                await self.events.set_cursor(f"sink:{sink.name}", last)

    async def deliver_once(self) -> dict[str, int]:
        """One round: report positions, then hand each sink what it has not had yet (side by
        side). Returns {sink name: events delivered}."""
        async with self._round:
            await self.events.store_pending()
            if self.events.pending:                      # the database is still refusing them
                await self._send_pending()
            try:
                await self.events.watch_positions()
            except Exception as e:
                logger.error(f"[Sinks] position watch failed: {e}")
            counts = await asyncio.gather(*(self._deliver(sink) for sink in self.sinks), return_exceptions=True)
            out: dict[str, int] = {}
            for sink, n in zip(self.sinks, counts, strict=True):
                if isinstance(n, BaseException):
                    logger.error(f"[Sinks] {sink.name}: round failed ({sink.redact(str(n))})")
                    n = 0
                out[sink.name] = n
            return out

    async def _send_pending(self) -> None:
        """Events the database refused go straight to each sink that wants them, once (a failed send
        is tried again next round)."""
        for p in list(self.events.pending):
            event = {"id": None, "kind": p["kind"], "severity": p["severity"], "message": p["message"],
                     "data": p["data"], "position_id": p["position_id"], "created_at": p["created_at"].isoformat()}
            for sink in self.sinks:
                if sink.name in p["sent_to"] or not sink.wants(p["kind"], p["severity"]):
                    continue
                if self.clock() < self._not_before.get(sink.name, 0):
                    continue
                try:
                    await sink.send(self.http, event)
                    p["sent_to"].append(sink.name)
                except Exception as raw:
                    logger.warning(f"[Sinks] {sink.name}: an unstored {p['kind']} event not delivered "
                                   f"({sink.redact(str(raw))}) - tried again next round")

    async def _deliver(self, sink: Sink) -> int:
        if self.clock() < self._not_before.get(sink.name, 0):
            return 0                                     # still waiting after a failure
        name = f"sink:{sink.name}"
        cursor = await self.events.cursor(name) or 0
        async with self.events.db.session() as s:
            rows = (await s.execute(select(Event).where(Event.id > cursor).order_by(Event.id).limit(BATCH))).scalars().all()
        sent, started, expected = 0, self.clock(), cursor + 1
        settled_before = datetime.now(UTC) - timedelta(seconds=GAP_WAIT_S)
        for e in rows:
            if e.id != expected and e.created_at > settled_before:
                break                                    # an earlier id may still be committing
            if self.clock() - started > ROUND_BUDGET_S:
                break
            if sink.wants(e.kind, e.severity) and sink.name not in ((e.data or {}).get("_sent_to") or []):
                event = {"id": e.id, "kind": e.kind, "severity": e.severity, "message": e.message, "data": e.data,
                         "position_id": e.position_id, "created_at": e.created_at.isoformat()}
                try:
                    await sink.send(self.http, event)
                    sent += 1
                    self._failures.pop(sink.name, None)
                except Exception as raw:
                    ex = sink.redact(str(raw))
                    if not (isinstance(raw, SendError) and raw.refused):
                        n = self._failures[sink.name] = self._failures.get(sink.name, 0) + 1
                        retry_after = raw.retry_after if isinstance(raw, SendError) else None
                        # A receiver's own wait is kept to BACKOFF_MAX_S: a huge Retry-After must
                        # not silence this sink for good.
                        wait = min(retry_after if retry_after is not None else 2 ** (n - 1), BACKOFF_MAX_S)
                        self._not_before[sink.name] = self.clock() + wait
                        logger.warning(f"[Sinks] {sink.name}: event {e.id} not delivered ({ex}) - "
                                       f"retried in {wait:.0f} s")
                        break
                    key = (sink.name, e.id)
                    self._refusals[key] = self._refusals.get(key, 0) + 1
                    if self._refusals[key] < MAX_ATTEMPTS:
                        logger.warning(f"[Sinks] {sink.name}: event {e.id} refused ({ex}) - retried next round")
                        break
                    logger.error(f"[Sinks] {sink.name}: event {e.id} refused {MAX_ATTEMPTS} times ({ex}) - skipped")
                    self._refusals.pop(key, None)
            await self.events.set_cursor(name, e.id)
            expected = e.id + 1
        return sent

    async def close(self) -> None:
        await self.http.aclose()
