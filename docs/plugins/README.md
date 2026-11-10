# Plugins

A plugin adds a source format, a notification sink or an exchange to the relay without editing
the relay. It is an ordinary Python package: you install it next to the relay with `pip install`,
and it registers itself under an entry point. The relay loads a plugin only when a config file
names it, and never in place of something built in. A plugin named `telegram` or `okx` is never
used, because those names belong to the shipped sink and the CCXT exchange (as long as CCXT is
installed, as it is in the Docker image: without it, a CCXT exchange's name is free).

| Kind | Entry point group | Named in | What it does | Guide |
|---|---|---|---|---|
| Source | `quanti_relay.sources` | a source file: `format: <name>` | translates a sender's own JSON into the v1 signal | [sources.md](sources.md) |
| Sink | `quanti_relay.sinks` | a sink file: `type: <name>` | delivers the relay's events somewhere | [sinks.md](sinks.md) |
| Venue | `quanti_relay.venues` | the deployment file: `venue.exchange: <name>` | connects an exchange CCXT does not reach | [venues.md](venues.md) |

Each kind has:
- a working template in `relay/plugins/_template_source.py`, `_template_sink.py` and
  `_template_venue.py`. Copy it into your package and change it.
- a contract check in `relay/plugins/contract.py` (`check_source`, `check_sink`, `check_venue`).
  Call it from your package's tests. A plugin that does not pass it is not ready.

## Trust

A plugin is code that runs inside the relay, with the relay's rights: it can read the relay's
environment, exchange keys included, and anything else the process can. The rules below are what
the relay enforces at its edges, not a sandbox. Install only plugins you have read or whose
authors you trust, and pin their versions.

## What the relay does not let a plugin do

- **Prove who sent a signal.** The relay checks the sender (its hmac signature, or the passphrase
  and address for TradingView) before a source plugin sees the payload. The plugin only
  translates, off the relay's main loop and within 5 seconds.
- **Hold a secret in a file.** Tokens, keys and URLs come from environment variables. A sink file
  holding one is refused, and every `*_env` variable it names must be set.
- **Choose trading numbers.** A source plugin passes on the size and stop a payload carries, and
  invents none. A venue plugin presets no size, stop or leverage.
- **Switch to live.** `dry_run`, `environment: live` and the trading settings belong to the
  deployment file and its owner.

## Registering a plugin

In your package's `pyproject.toml`:

```toml
[project.entry-points."quanti_relay.sinks"]
pager = "relay_pager:PagerSink"
```

After `pip install relay-pager` next to the relay, `type: pager` in a sink file uses it.
`relay check-config` reports a plugin it cannot load, and a file that names one stops the start.
