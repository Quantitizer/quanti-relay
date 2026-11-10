"""Template: a sink plugin (docs/plugins/sinks.md).

A sink plugin delivers the relay's events somewhere the built-in sinks (Telegram, Discord,
webhook) do not reach. The relay decides which events a sink gets (the sink file's min_severity
and kinds), keeps its place in the event list, and retries a failed send: the plugin only sends
one event and says how it went.

This template posts each event as JSON to a URL with a bearer token, both read from environment
variables the sink file names:

    # config/sinks/pager.yaml
    type: json-post                  # the name the plugin is registered under
    min_severity: warning
    url_env: RELAY_PAGER_URL
    token_env: RELAY_PAGER_TOKEN

Registering it, in your package's pyproject.toml:

    [project.entry-points."quanti_relay.sinks"]
    json-post = "your_package.module:JsonPostSink"

Rules for a sink:
- Secrets only from the environment, through settings ending in `_env` (the relay refuses a sink
  file that holds a token or URL itself, and checks every `_env` variable is set).
- `send` raises relay.sinks.SendError on failure: refused=True when the receiver refuses this
  event outright (it is retried a few times, then skipped), otherwise it is retried later, after
  retry_after seconds when the receiver says so.
- `redact` hides the secrets from any text (the relay logs errors through it).
"""
from __future__ import annotations

from typing import Any

import httpx

from relay.sinks import SendError


class JsonPostSink:
    def __init__(self, name: str, url: str, token: str):
        self.name, self._url, self._token = name, url, token

    @classmethod
    def from_config(cls, name: str, settings: dict[str, Any], environ) -> JsonPostSink:
        unknown = set(settings) - {"url_env", "token_env"}
        if unknown:
            raise ValueError(f"unknown setting(s) {sorted(unknown)}")
        for key in ("url_env", "token_env"):
            if not settings.get(key):
                raise ValueError(f"{key} must name the environment variable that holds it")
        url, token = environ.get(settings["url_env"], ""), environ.get(settings["token_env"], "")
        if not url.startswith("https://"):
            raise ValueError(f"the URL in {settings['url_env']} must start with https://")
        if not token:
            raise ValueError(f"{settings['token_env']} is not set")
        return cls(name, url, token)

    async def send(self, http: httpx.AsyncClient, event: dict[str, Any]) -> None:
        try:
            r = await http.post(self._url, json=event, headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as e:
            raise SendError(f"no answer: {type(e).__name__}") from e
        if r.status_code >= 300:
            refused = 400 <= r.status_code < 500 and r.status_code not in (408, 429)
            wait = r.headers.get("Retry-After")
            raise SendError(f"answered {r.status_code}", refused=refused,
                            retry_after=float(wait) if wait and wait.isdigit() else None)

    def redact(self, text: str) -> str:
        for secret in (self._url, self._token):
            text = text.replace(secret, "***")
        return text
