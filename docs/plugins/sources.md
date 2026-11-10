# A source plugin, step by step

Use one when a sender posts its own JSON and cannot be changed to post the relay's v1 signal
(`schemas/signal.v1.json`). The plugin turns that JSON into a v1 signal. Everything else stays
with the relay:
- checking the sender
- the signal id that is never carried out twice
- the rails and the deployment's rules

## 1. Start from the template

Copy `relay/plugins/_template_source.py` into your package. It reads an imagined "simple alert":

```json
{"id": "a-123", "deployment": "main", "time": "2026-11-03T12:00:00Z",
 "ticker": "BTCUSDT", "action": "buy", "usd": 500, "stop_pct": 2}
```

and returns the v1 event signal for it. Change `translate` for your sender's format:

- **Return a v1 body:** `version`, `id`, `deployment`, `sent_at`, `kind`, then the event or target
  fields. Leave out `source`: the relay fills it in with the source the request was checked
  against, whatever the plugin returns.
- **Carry the sender's own id into `id`.** It is what stops a repeat from being carried out twice.
- **Invent no numbers.** A size, stop or take-profit the payload does not carry stays out. The
  deployment's rules then apply, or the relay refuses the signal (an `open` needs a size).
- **Raise `ValueError` with a plain message for a payload you cannot read.** The relay answers 422
  with it, and nothing is carried out.
- **No I/O and no state.** The same payload always gives the same signal.

## 2. Check it

In your package's tests:

```python
from relay.plugins.contract import check_source

def test_contract():
    check_source(MySource(), samples=[...payloads you read...], unreadable=[...payloads you refuse...])
```

It checks that:
- every sample becomes a valid v1 signal, the same one every time, without changing its input;
- every unreadable payload is refused with `ValueError`.

## 3. Register and use it

```toml
[project.entry-points."quanti_relay.sources"]
my-format = "my_package.module:MySource"
```

The relay's source file names the format. Authentication works as for any source:

```yaml
# config/sources/vendor.yaml
auth: hmac                         # or passphrase (TradingView-style), with allow_ips
secret_env: RELAY_SOURCE_VENDOR_SECRET
deployments: [main]
format: my-format
```

The sender posts its JSON to `POST /v1/sources/vendor/signals`. The source is named in the path,
because the sender's JSON does not carry a `source` field. With `auth: hmac` the request carries
`X-Relay-Signature` over the raw body, exactly as for `/v1/signals`.
