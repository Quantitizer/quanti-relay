# Quanti Relay

Self-hosted, open-source signal-to-trade relay. Receives trading signals as JSON (from your own
systems, TradingView alerts, or any other source), applies configurable risk rails, and executes
them on crypto exchanges.

**Status: early release. Start on a test account; live trading is at your own risk.**

- **Signals in:** a signed JSON contract (`schemas/signal.v1.json`) for single trades and whole
  target books; TradingView alerts; or any format through a source plugin.
- **Rails:** limits on size, exposure and leverage, a daily loss limit, and the stop rule. Each is
  an empty setting you fill in, and an empty one is off.
- **Execution:**
  - Bybit natively.
  - Any exchange CCXT reaches with USDT perpetuals, or spot (`docs/venues.md`).
  - Anything else through a venue plugin (`docs/plugins/`).
- **Safety:**
  - Stops rest on the exchange.
  - Every position is reconciled with the venue.
  - It runs on paper (dry-run) until its owner switches that off.
- **Out:** events to Telegram, Discord or a webhook, and a small console at `/console`.

## On your own server, set up by your AI

Tell your AI agent (or chat app) to set Quanti Relay up for you, and point it at
`docs/setup-playbook.md`. It walks you through your own cloud account (any provider; `docs/cloud.md`
lists recommended ones), creates the server with `deploy/install.sh` (run from a clone made by
root, e.g. `sudo git clone https://github.com/Quantitizer/quanti-relay.git /opt/quanti-relay`), and guides you through the
exchange key, the limits and the signals. A chat app without a terminal works too: it guides you
through your provider's website and its browser console. Your secrets go from you straight into
the server, never through the chat, and going live stays your own step.

## Quickstart (Docker)

```sh
git clone https://github.com/Quantitizer/quanti-relay.git && cd quanti-relay
cp .env.example .env                       # set RELAY_API_KEY in it
mkdir -p config/deployments config/sources config/sinks
docker compose up -d
```

Then open http://127.0.0.1:8080/console, enter the API key and follow the Setup tab (or copy
`examples/config/` into `config/` and edit the files by hand). The relay listens on this machine
only. To reach it from elsewhere, put a TLS reverse proxy in front and name it in
`RELAY_TRUSTED_PROXIES`; `docs/SPEC.md` §4 lists what guards a relay on the internet (the
wrong-key lockout, an optional address allowlist, `/health` and the heartbeat, database copies).

It starts on paper: no order reaches an exchange. Check what it will do with:

```sh
docker compose exec relay relay check-config
```

Send a test signal from a source you defined in `config/sources/` (examples in
`examples/config/sources/`):

```sh
docker compose exec relay relay test-signal --source <name> --action open --symbol BTCUSDT --side long --notional <USDT> --stop-pct <PERCENT>
```

Before trading on an exchange, run its connector through `relay verify-venue` on a demo or test
account (`docs/venues.md`).

## Setting up in the console

The console (`/console`, Setup tab) sets the relay up without editing files: the exchange, the
limits, the signal sources and the notifications. It writes the same files you would, after the
same checks the relay makes at start, and keeps every version it replaced in `config/.history/`.

- **Keys never go into the console.** It tells you which variable to add to `.env` and whether it
  is set; you add the line and make the container again so it reads `.env` (`relay restart` on a
  server set up by `deploy/install.sh`, or `docker compose up -d`). The console's Restart button
  applies changed config files, not `.env`.
- **On Linux,** give the config folder to the container's user once, so the relay may write it:
  `sudo chown -R 10001:10001 config`. On macOS and Windows (Docker Desktop) nothing is needed.
- **Going live** takes two keys: the console writes `dry_run: false` into the deployment file only
  when the exchange keys are set, the core limits are filled in, the venue check has passed on the
  exchange's test account, and you type the deployment's name; then you set `RELAY_DRY_RUN=false`
  in `.env` and restart. Back to paper is one click and applies at once.

## Without Docker

Python 3.11 and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --extra venues
export RELAY_API_KEY=...                   # keys and secrets only ever from the environment
uv run relay check-config
uv run relay serve
```

## Documentation

- `docs/SPEC.md`: what the relay is and does, the signal contract, the rails.
- `docs/connectors.md`: every way in and out (signals, exchanges, notifications, the API), what
  each can do, and its current limitations.
- `docs/cloud.md`: running it on a server in your own cloud, with any provider.
- `docs/setup-playbook.md`: the steps for an AI agent setting the relay up for someone.
- `docs/venues.md`: the exchanges, their environments, spot, and how a connector is verified.
- `docs/plugins/`: writing a source, sink or venue plugin.
- `AGENTS.md`: the layout, the commands and the rules, for contributors and coding agents.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
