# A sink plugin, step by step

Use one to deliver the relay's events somewhere the built-in sinks (Telegram, Discord, webhook)
do not reach, such as a pager, a chat or a log service. The relay keeps the hard parts:
- which events a sink gets (`min_severity`, `kinds`)
- each sink's place in the event list, so nothing is lost over a restart
- retrying a receiver that is down

The plugin sends one event and says how it went.

## 1. Start from the template

Copy `relay/plugins/_template_sink.py`. It posts each event as JSON to a URL with a bearer token.

- **`from_config(name, settings, environ)`** builds the sink. `settings` is the sink file without
  `type`, `enabled`, `min_severity` and `kinds`. Read secrets from `environ`, through settings
  ending in `_env`. Raise `ValueError` for settings you cannot use: the relay then stops at start
  and names the file.
- **`send(http, event)`** delivers one event:
  `{"id", "kind", "severity", "message", "data", "position_id", "created_at"}`. `http` is the
  relay's `httpx.AsyncClient`. On any failure raise `relay.sinks.SendError`:
  - `refused=True` when the receiver refuses this event outright (a 4xx other than 408/429). It
    is retried a few times, then skipped, so one bad event cannot block the sink.
  - otherwise (no answer, 5xx, 408, 429) it is retried later. Pass `retry_after` when the
    receiver says how long to wait.
- **`redact(text)`** returns the text without your secrets. The relay logs failures through it,
  and also hides the values of every `*_env` variable itself.

## 2. Check it

```python
from relay.plugins.contract import check_sink

async def test_contract():
    await check_sink(MySink, settings={"url_env": "X_URL", "token_env": "X_TOKEN"},
                     environ={"X_URL": "https://example.test/hook", "X_TOKEN": "..."})
```

It answers your sink with 200, 400, 404, 408, 429 (with Retry-After), 500, 503 and no answer at
all, and checks:
- what `send` raises in each case;
- that settings without their variables are refused;
- that `redact` hides the secrets.

It is written for sinks that deliver over HTTP through the `http` client they are given.

## 3. Register and use it

```toml
[project.entry-points."quanti_relay.sinks"]
pager = "my_package.module:PagerSink"
```

```yaml
# config/sinks/pager.yaml
type: pager
min_severity: warning
url_env: RELAY_PAGER_URL
token_env: RELAY_PAGER_TOKEN
```

A sink file that holds a secret itself is refused, as is one naming a variable that is not set.
Any setting whose name suggests a secret (a token, key, URL, webhook, endpoint, DSN, password)
must end in `_env`. The plugin's `from_config` receives only the variables its file names, never
the relay's own keys. A sink with `enabled: false` is not loaded at all. The relay gives a
`send` 20 seconds; one that takes longer, or raises anything other than `SendError`, counts as a
failure and is retried later.
