# Connectors: what goes in, what goes out

This page lists every way into the relay and every way out of it, what each one can do, and
where each one stops. It was checked against the code on 2026-11-04 (CCXT 4.5.89).

The relay ships no trading numbers. Where a size, stop or limit appears below, it is written as
`<USDT>` or `<PERCENT>`: you fill it in, and an empty setting is off.

- [1. Signals in](#1-signals-in)
- [2. Execution venues](#2-execution-venues)
- [3. Notifications out](#3-notifications-out)
- [4. Read APIs and the console](#4-read-apis-and-the-console)
- [5. Current limitations](#5-current-limitations)

---

## 1. Signals in

### 1.1 Overview

| Way in | Route | Who proves it | What it accepts | Logged as a signal |
|---|---|---|---|---|
| hmac source | `POST /v1/signals` | `X-Relay-Signature` over the raw body | a v1 signal (event or target) | yes |
| passphrase source (TradingView) | `POST /v1/signals` | `passphrase` in the body, and the sender's address on `allow_ips` | a v1 signal | yes |
| any source, named in the path | `POST /v1/sources/{name}/signals` | as above, by the source's `auth` | a v1 signal without `source`, or the sender's own JSON when the source has `format:` | yes |
| source plugin | the path route above | the relay, before the plugin runs | anything the plugin turns into a v1 signal | yes |
| REST order | `POST /v1/orders` | the full API key | one entry (an `open`) | yes, under source `api` |
| position actions | `POST /v1/positions/{id}/...` | the full API key | close, reduce, stop, trailing, break-even, take-profits | no |
| close-all, emergency stop | `POST /v1/close-all`, `POST /v1/emergency-stop` | the full API key (the console asks you to type the deployment name) | nothing | no |
| `relay test-signal` | sends to `POST /v1/signals` | signs with an hmac source's secret | one event or one target book, always `dry_run: true` | yes |

### 1.2 The v1 signal contract

`schemas/signal.v1.json` is generated from `relay/signals/contract.py`, and a test keeps them the
same. Every part is strict: an unknown field is refused, and so are `true` where a number belongs,
Infinity and NaN.

**Envelope (every signal)**

| Field | Required | Rule |
|---|---|---|
| `version` | yes | `"1"` |
| `id` | yes | 1 to 100 characters, unique per signal. A repeat from the same source is answered `duplicate` and never carried out twice; the same id with a different signal is refused (409). |
| `source` | yes on `/v1/signals` | 1 to 50 characters, a registered source. On the path route it is filled in from the path. |
| `deployment` | yes | 1 to 50 characters. Must be the running deployment, and the source must be bound to it. |
| `sent_at` | yes | ISO-8601 with a time zone. |
| `ttl_s` | no | Seconds; default 60. Older than `sent_at + ttl_s` is refused as stale. More than 30 s in the future is refused. |
| `kind` | yes | `event` or `target` |
| `dry_run` | no | `true` makes this signal paper. It can never make a paper deployment live. |

There is no `meta` field. A signal that carries one is refused.

**Event signals (`kind: "event"`)**

| Action | Needs | May carry | Not allowed |
|---|---|---|---|
| `open` | `symbol`, `side`, `size` (one of `notional_usd`, `equity_pct`, `risk_pct`, `qty`) | `stop` (`price` or `pct`), `take_profit` ladder, `trailing`, `break_even`, `execution` | |
| `flip` | as `open` | as `open` | |
| `close` | `symbol` | `side` (to pick one of two positions) | size, stop, management |
| `reduce` | `symbol`, `size: {close_pct}` (more than 0, at most 100) | | side, stop, management |
| `close_all` | nothing | | symbol is optional and ignored |
| `move_stop` | `symbol`, `stop: {price}` | | `stop.pct`, management |

- `stop` is required for an entry while the deployment has `require_stop: true` (the default).
- `take_profit`, `trailing` and `break_even` each accept `false`: no such rule for this order,
  even if the deployment sets one.
- `execution` accepts `market`, `limit` and `maker_chase`, but only `market` is carried out.
  The others are recorded as skipped (`execution_not_supported`).

**Target signals (`kind: "target"`)**

| Field | Rule |
|---|---|
| `weights` | `{symbol: signed fraction of equity}`. Symbols the book holds but leaves out go to zero. Two keys that are the same symbol are refused. |
| `gross_leverage` | optional, more than 0: multiplies the weights before the rails |
| `stop_pct` | optional, between 0 and 100: the stop of every new leg. Required when `require_stop` is on. |
| `execution` | market only, as above |

One target signal runs at a time. It is carried out margin first (exits, reductions, flips,
increases, entries). It trades nothing when the book is not settled: a close in progress, two
positions on one symbol, or (live) a size that differs from the venue's.

**Symbols.** The contract reads `BTC/USDT:USDT`, `BTCUSDT`, `BTCUSDT.P` and `BYBIT:BTCUSDT.P`.
`BTC/USDT` (no settle coin) is a spot pair and is refused. USDC symbols pass the contract, but the
executors only carry out USDT symbols: `BTC/USDC:USDC` is skipped (`unsupported_symbol`). On a
USDC venue (Hyperliquid), send `BTCUSDT`; the connector trades the USDC perpetual.

### 1.3 Sources and how they authenticate

A source is one file in `config/sources/<name>.yaml` (the console's Setup tab writes it for you).
Allowed keys: `name`, `auth`, `secret_env`, `deployments`, `enabled`, `max_signals_per_minute`,
`allow_ips`, `client_ip_header`, `trusted_proxies`, `format`, `actions`. Anything else, and a
`secret` key, stops the start. The names `api` and `console` are the relay's own.

`actions` limits what a source may send: any of `open`, `close`, `reduce`, `flip`, `close_all`,
`move_stop`, and `target` (a whole book). Left out, it may send everything. A TradingView source
that only opens and closes cannot be used to close everything or move stops, even if its
passphrase leaks.

| | hmac | passphrase |
|---|---|---|
| Meant for | your own systems | TradingView, which cannot sign |
| Secret | `secret_env` names an environment variable; at least 32 characters | the same |
| Proof | `X-Relay-Signature: t=<unix>,v1=<hex>`, HMAC-SHA256 over `<t>.<raw body>`. Several `v1=` values may be sent (for rotating a secret). | `"passphrase"` in the JSON, compared in constant time, removed before the signal is read or stored |
| Address check | none | `allow_ips` is required (IPs or CIDR ranges) |
| Behind a proxy | not needed | `client_ip_header` plus `trusted_proxies` (both required together). The header is believed only from a trusted proxy, and read from the right. |
| Replay | signature older or newer than 30 s is refused; inside that window the `id` stops a repeat | only `ttl_s`, `sent_at` and the `id` |

The secret is read from the process environment. Under Docker, `compose.yaml` passes the `.env`
file into the container's environment, so the values set there are used. Without Docker the
relay does not read `.env` for source secrets.

### 1.4 The answers, in order

The relay checks a request in this order. The first problem decides the answer.

| Step | Answer | Logged as a signal |
|---|---|---|
| body over 64 KiB (by header or while reading) | 413 | no |
| not JSON, or not a JSON object | 400 | no |
| unknown source, bad or old signature, wrong passphrase or address | 401, one answer for all | no (a log line at most every 10 s) |
| source has `enabled: false` | 403 | no |
| path route: the body names another source (no `format`) | 422 | no |
| the source plugin cannot read the payload | 422 | no |
| not a valid v1 signal | 422 with the reasons | no |
| 64 signals already running | 503, send it again | no |
| same `id` seen before from this source, same signal (a retry; `sent_at` may differ) | 202 `duplicate` | already logged |
| same `id`, a different signal | 409 `conflict`, and a `signal.conflict` event | the first one only |
| over `max_signals_per_minute` (counted only for correctly signed, new signals) | 429 | no |
| source not bound to the deployment, or not allowed this action (`actions`) | 403 | yes, `rejected` |
| unknown deployment, stale, or from the future | 422 | yes, `rejected` |
| accepted | 202 `accepted`; carried out in the background | yes |

The outcome (`executed`, `skipped` with a reason, `failed`) is read at
`GET /v1/signals/{signal}` and is also an event (`signal.executed`, `signal.skipped`,
`signal.failed`). A signal cut off by a shutdown is marked `failed` (`interrupted`).

### 1.5 Source plugins

A source file with `format: <name>` loads the installed plugin of that name (entry point group
`quanti_relay.sources`). The relay checks the sender first, then calls `translate(payload)`.

- The plugin's output gets the source's own name in `source`, whatever the plugin wrote, and any
  `passphrase` is removed.
- A plugin that raises, or returns something that is not an object, gives 422 and nothing is
  logged.
- The sender posts to `POST /v1/sources/{name}/signals`, because its JSON has no `source`.
- `relay.plugins.contract.check_source` checks that each sample becomes a valid v1 signal, the
  same every time, without changing its input, and that unreadable payloads raise `ValueError`.

### 1.6 Other ways in

**`POST /v1/orders` (full API key).** One entry. `symbol` must be letters and digits only (no
`BTC/USDT:USDT`), 2 to 30 characters. `size` has exactly one of `notional_usd`, `equity_pct`,
`risk_pct`, `qty`. It takes `stop_price` (no stop percentage), `take_profit_price` or a
`take_profits` ladder, `leverage`, `trailing`, `break_even`, and `client_id` (up to 100
characters) as the repeat key. It answers synchronously: 200, 409, 422, 500, 502 or 503 (the
module text in `relay/api/app.py` says which means what).

**Position actions (full API key):** `close`, `reduce` (`close_pct`), `stop` (`stop_price`),
`trailing` (POST sets, DELETE removes), `break-even` (POST, DELETE), `take-profits` (POST,
DELETE). Housekeeping: `POST /v1/sync`, `/v1/stop-audit`, `/v1/pnl-audit`, `/v1/stop-rules`,
`/v1/circuit-breaker/reset`, `/v1/resume`.

**Close-all and the emergency stop.** `close-all` closes every position of the deployment, paper
and live, at market, and leaves the relay running. `emergency-stop` first refuses new entries
(event `relay.halted`), waits for entries already under way, closes every position, and stops the
engine. The halt is kept in the database: a restart does not lift it. `POST /v1/resume` (or Resume in
the console) does. Both stop calls answer 502 when something is still open.

**The console's test signal** (`POST /v1/setup/test-signal`, full API key). An event or a book,
always on paper, logged under the source `console`: it runs through the checks, the limits and the
simulated fill like any other signal, without a sender's signature.

**`relay test-signal`.** Builds an event (`--action`, `--symbol`, `--side`, `--notional`,
`--close-pct`, `--stop-pct` or `--stop-price`) or a target book (`--weights`, `--stop-pct`,
`--gross-leverage`), always with `dry_run: true`, signs it with the source's secret, and posts it
to `--url` (default `http://127.0.0.1:8080`). `--print-only` prints the signed request instead.
It works with hmac sources only, and without a source plugin.

**The owner's limits, for a sender to read** (`GET /v1/sources/<name>/limits`, an hmac source's own
signature, no API key). A platform that builds trades for the owner reads them first. The signature
is made like a signal's, over `<t>.GET /v1/sources/<name>/limits` in place of a body, with the same
±30 s window. The answer: `schema` (1), `source`, `deployment`, `as_of`, `mode` (`paper` or
`live`), `halted`, `entries_enabled`, `venue` (`exchange`, `market`, `environment`) and `limits`:
`max_notional_usd`, `max_notional_pct`, `max_gross_leverage`, `on_limit`, `max_positions`,
`max_exposure_pct`, `daily_loss_limit_pct`, `min_stop_distance_pct`, `max_stop_distance_pct`,
`max_hold_hours`. `null` means the rule is off. The largest loss one trade can make is its size
times its stop distance, so `max_notional_*` with `max_stop_distance_pct` bounds it, and
`daily_loss_limit_pct` bounds the day. This is a preview: the relay applies every limit itself,
whatever the sender read. Refused like a signal: 401 (unknown source, a passphrase source, or a
bad or old signature, one answer for all), 403 (disabled, or not bound to the running deployment).

---

## 2. Execution venues

### 2.1 How a venue is chosen

| `venue.exchange` | `venue.market` | Connector | `venue.environment` |
|---|---|---|---|
| `bybit` | empty (perpetual) | native Bybit | must be empty: `RELAY_BYBIT_DEMO` / `RELAY_TESTNET` decide (testnet by default) |
| `bybit` | `spot` | CCXT spot | required |
| a CCXT exchange id | empty | CCXT perpetuals | required: `demo`, `testnet` or `live`, where offered |
| a CCXT exchange id | `spot` | CCXT spot (needs `dedicated_account: true`) | required |
| any other name | empty | the installed venue plugin of that name | passed to the plugin |

A start is refused while live positions are open on another venue or environment.

### 2.2 Keys

| Connector | Variables |
|---|---|
| native Bybit (perpetuals) | `BYBIT_API_KEY`, `BYBIT_API_SECRET` (no `RELAY_` prefix) |
| CCXT, key and secret | `RELAY_<EXCHANGE>_API_KEY`, `RELAY_<EXCHANGE>_API_SECRET` |
| CCXT with a passphrase (profiles `okx`, `bitget`, `kucoinfutures`) | also `RELAY_<EXCHANGE>_API_PASSPHRASE` |
| CCXT Hyperliquid | `RELAY_HYPERLIQUID_WALLET_ADDRESS`, `RELAY_HYPERLIQUID_PRIVATE_KEY` |
| venue plugin | read by the plugin from the environment |

On paper no keys are needed: the relay reads prices and market rules only, and the balance it
sizes with is `paper_equity` from the deployment file (USDT, your own number; empty means paper
entries are refused until keys are set). Live without the keys is refused at start. An exchange without a profile reads the credentials CCXT says it needs:
`RELAY_<EXCHANGE>_API_PASSPHRASE`, `_API_UID`, `_API_LOGIN` or `_API_TOKEN` as well where it needs
them.

### 2.3 Capability matrix: perpetuals

"Stop rests" says where the relay's stop lives. "Ladder" is a take-profit ladder of resting
reduce-only limits. "Lev." and "Margin" are whether `leverage` and `isolated_margin` can be set
through the API (if not, the start is refused while they are set). "PnL" is whether a close the
exchange made (a stop that fired) is priced from the exchange's own fills. "Funding" is whether
funding payments are read.

| Exchange | Id | Environments | Stop rests | Trailing | Ladder | Lev. | Margin | PnL | Funding | Tier |
|---|---|---|---|---|---|---|---|---|---|---|
| Bybit | native | demo, testnet, live | on the position | yes | yes | yes | yes | yes | yes | verified on demo |
| Binance USDⓈ-M | `binance` | demo, testnet, live | algo order, mark price | yes | yes | yes | yes | yes | yes | community |
| OKX | `okx` | testnet (demo trading), live | conditional algo order, mark price | yes | yes | yes | yes | yes | yes | community |
| Bitget | `bitget` | demo, live | plan order (`profit_loss`) | yes | yes | yes | yes | yes | no (see 5) | community |
| Gate | `gate` | testnet, live | trigger order | no | yes | yes | no | no | yes | community |
| MEXC | `mexc` | live only | plan order, renewed before its 7-day life ends | no | yes | no | no | no | yes | community |
| KuCoin futures | `kucoinfutures` | live only | trigger order | no | yes | yes | yes | no | no (see 5) | community |
| HTX | `htx` | live only | stop algo order (own list) | yes | yes | yes | no | no | yes | community |
| BingX | `bingx` | testnet (VST), live | among plain open orders | yes | yes | yes | yes | no | yes | community |
| Hyperliquid | `hyperliquid` | testnet, live | among plain open orders | no | yes | yes | yes | yes | yes | community, USDC |
| any other CCXT exchange | its id | testnet if CCXT knows a test host, live | a reduce-only stop-market order, if CCXT lists stop orders | no | yes | if CCXT can | if CCXT can | no | no | community |

Common to every CCXT perpetual:

- **Short:** yes.
- **Quantities** are coins in the relay and converted with the market's contract size, in decimal.
- **One-way mode** is set at start where CCXT can; otherwise a warning asks you to set it.
- **Moving a stop** places the new order before it cancels the old one.
- **A stop is shown on the position** only when it covers the whole position. The stop audit
  re-protects a position that grew.
- **Tags.** The relay's orders carry its tag, rewritten per exchange (OKX letters and digits,
  Gate `t-`, HTX an 18-digit number, Hyperliquid a 128-bit hex). At the first stop the relay
  checks that the exchange gives the tag back; if not, stops are refused there from then on.
- **Single take-profit** (`take_profit_pct`) is a resting take-profit trigger order.
- **Break-even** is the relay's own loop: it moves the stop when the price gets there.
- **Trailing on a venue without one:** a deployment setting stops the start; a signal's
  `trailing` is reported as not set, and the position keeps its fixed stop.
- **No stop orders at all** (13 CCXT exchanges with perpetuals, such as `xt`): the relay starts
  only with `require_stop: false`, refuses any signal that asks for a stop, and refuses
  `take_profit_pct` and break-even at start.

### 2.4 Capability matrix: spot

| | Spot through CCXT |
|---|---|
| Exchanges | any CCXT exchange with USDT spot markets, Bybit included; not HTX |
| Venue id | `<exchange>spot` (for example `binancespot`) |
| Account | `dedicated_account: true` is required: every coin held above dust is a position |
| Side | long only; a short, a flip to short or a book with short legs is refused before anything closes |
| Stop | a stop-market sell resting on the exchange, where CCXT's feature table says it can (Binance, Bybit, OKX, Bitget, Gate, KuCoin, BingX, Kraken, Coinbase). MEXC and Hyperliquid spot: only with `require_stop: false`. |
| Moving the stop | old one cancelled first (it holds the coins), then the new one; the old one is put back if the new one is refused |
| Take-profit, ladder, trailing | refused at start; a take-profit sent with a signal is not placed |
| Leverage, margin | refused at start |
| PnL | none from the exchange; a close the exchange makes stays unpriced |
| Funding | none (empty, not unknown) |
| Entry price | average cost of the latest buys that make up the holding |

### 2.5 Venue plugins

A name in `venue.exchange` that is neither `bybit` nor a CCXT id loads the installed plugin
(group `quanti_relay.venues`). `build(environment, *, paper, environ)` returns a client that:

- answers every call in `docs/venues.md` ("The calls"), in Bybit's shape;
- reports `venue_id` equal to the registered name (2 to 30 lowercase letters and digits);
- says `can_stop` and `can_short` as `True` or `False` (required);
- may say `can_ladder`, `can_trail`, `can_set_leverage`, `can_set_margin_mode`, `gives_pnl`
  (missing means no, and the matching deployment settings are refused at start).

A plugin trades its own markets: `venue.market: spot` is refused for it. Every start warns that
a plugin is untested until `relay verify-venue` passes.

### 2.6 `relay verify-venue`

`relay verify-venue --symbol <SYMBOL> --notional <USDT> --stop-pct <PERCENT> [--confirm]`

- Runs on the deployment's CCXT exchange or venue plugin. Refuses native Bybit and
  `environment: live`. Without `--confirm` it only says what it would do.
- Steps: read, open, stop, move, same, ladder (spot: partial sell), restart, close, records,
  funding. After a failure the rest are skipped, and it always closes its position and removes
  its orders. If it cannot, it says so in capitals.
- It refuses a symbol that already holds a position.
- Not checked: a short, a trailing stop, a single take-profit order, leverage and margin calls.
- Exchanges with no test environment (MEXC, KuCoin futures, HTX) cannot be verified this way.

---

## 3. Notifications out

### 3.1 Capability matrix

| Sink | `type` | Settings (all `*_env` name environment variables) | What it sends | Size cap |
|---|---|---|---|---|
| Telegram | `telegram` | `bot_token_env`, `chat_id_env` | text: icon, severity, kind, message | 4000 characters |
| Discord | `discord` | `webhook_url_env` (must be https) | the same text | 1900 characters |
| Webhook | `webhook` | `url_env` (must be https), optional `secret_env` | JSON `{id, kind, severity, message, data, position_id, created_at}`; with a secret, signed `X-Relay-Signature` like an incoming signal | none |
| Plugin | the plugin's name | the plugin's own; anything that sounds like a secret must end in `_env` | whatever the plugin sends | 20 s per send |

Every sink file also takes `enabled`, `min_severity` (`info`, `warning`, `critical`; default
`info`) and `kinds` (exact kinds, or prefixes ending in `.`; empty means all).

### 3.2 Delivery

- Each sink keeps its own place in the event list, in the database, so a restart loses nothing.
  A new sink starts after the events that already exist.
- Sinks are served side by side every `notify_interval_s` (default 5 s), up to 50 events and 30 s
  per round each.
- **Down or busy** (no answer, 5xx, 408, 429, a timeout, any other error): the sink waits and
  tries the same event again. The wait doubles up to 300 s, or follows the receiver's
  Retry-After (also kept to 300 s). The event is never dropped.
- **Refused** (a redirect, or any other 4xx): tried again next round; after five refusals that
  one event is skipped. A redirect is not followed: put the final URL in the variable.
- **The database is down:** warning and critical events that cannot be stored are kept in memory
  and sent straight to the sinks, then stored when the database is back (without being sent
  twice).
- Secrets are hidden in log lines (the sink's own values, and a plugin's `redact`).
- Notifications only: nothing a sink receives can act on the relay.

### 3.3 Event kinds

| Kind | Severity |
|---|---|
| `relay.started` | info |
| `relay.halted` | critical |
| `relay.resumed` | warning |
| `signal.executed`, `signal.skipped`, `signal.rejected` | info |
| `signal.failed`, `signal.conflict` | warning |
| `position.opened`, `position.closed` | info |
| `position.stop_failed` | critical when the position may be open without a stop, else warning |
| `position.drift_detected`, `position.drift_repaired` | warning |
| `position.naked` (open without a stop, and the stop audit could not set one) | critical |
| `position.drift_unrepairable`, `position.unwatched` | critical |
| `config.changed` (critical when the file is set to live) | warning |
| `venue.checked` (a venue check from the console) | info when passed, else warning |
| `auth.locked` (an address sent too many wrong keys or tokens) | warning |
| `relay.stuck` (a background loop ended or hangs) | critical |

---

## 4. Read APIs and the console

### 4.1 Endpoints

The read-only key (`RELAY_API_READONLY_KEY`) may use every GET. The full key (`RELAY_API_KEY`)
may use everything. Keys go in `Authorization: Bearer <key>` or `X-API-Key`. With no key set, the
API answers 503 unless `RELAY_ENV=development`.

| Endpoint | Key | What it returns |
|---|---|---|
| `GET /health` | none | `{"service": "quanti-relay", "status": "ok"}`, or 503 with `starting` / `halted` / `stuck` (the reason and the loops: `GET /v1/status`, `health`) |
| `GET /docs`, `GET /openapi.json` | none | the API description (every call still needs a key) |
| `GET /console` and its script and style | none | the console page (it holds no data) |
| `GET /v1/status` | read | version, deployment, paper or live, venue and environment, engine, halt reason, Bybit live feed, circuit breaker |
| `GET /v1/positions` | read | positions; `status` open / closed / all, `mode` paper / live / all, `deployment_id`, `limit` (up to 1000), `offset` |
| `GET /v1/signals` | read | the signal log, newest first; `limit`, `status` |
| `GET /v1/signals/{id}` | read | one signal with its verdict and result |
| `GET /v1/events` | read | events, newest first; `limit`, `kind` (exact) |
| `GET /v1/settings` | read | the trading settings in force (read-only here) |
| `GET /metrics` | read | Prometheus text: paper flag, open positions by mode, Bybit call failures |
| `GET /v1/setup` | read | what is set up, from the files as they are now: exchange, keys (set or not, never their values), limits, sources, sinks, the last venue check, the gates to go live, and whether a restart is due |
| `GET /v1/setup/catalog` | read | the exchanges on offer and what each can do |
| `GET`, `PUT /v1/setup/{deployment,source,sink}/{name}`, `DELETE` (source, sink) | full | read and write a config file, after the checks the relay makes at start; the old version goes to `config/.history/`. A file may not name the relay's own key variables. |
| `POST /v1/setup/venue-check` | full | `relay verify-venue` on the exchange's demo or test account (never live), recorded |
| `POST /v1/setup/live` | full | writes `dry_run: false` once every gate is open and the deployment's name is typed; `RELAY_DRY_RUN=false` and a restart are still needed |
| `POST /v1/setup/paper` | full | back to paper, at once and in the file |
| `POST /v1/setup/restart` | full | stops the relay the normal way (Docker starts it again; `.env` is read again only when the container is made again: `relay restart`) |
| `GET /v1/oauth/clients`, `DELETE /v1/oauth/clients/{id}` | read / full | the chat apps connected to the MCP endpoint; removing one ends its tokens at once |

### 4.3 The MCP endpoint for chat apps (`/mcp`)

Off unless `RELAY_MCP_ENABLED=true`. A chat app that supports remote MCP connectors (custom
connectors) adds `https://<address>/mcp`; it registers itself and sends the owner to the relay's own
approval page, where the owner types the API key (OAuth 2.1 with PKCE; `relay/api/oauth.py`).

| Tool | Permission | What it does (through the API route named) |
|---|---|---|
| `relay_status`, `list_positions`, `list_events`, `list_signals` | read | the status and the lists, as the GET routes |
| `setup_show`, `setup_catalog` | read | `GET /v1/setup`, `GET /v1/setup/catalog` |
| `setup_write`, `setup_remove` | setup | `PUT` / `DELETE /v1/setup/...`: the same checks; never switches to live |
| `restart_relay` | setup | `POST /v1/setup/restart` |
| `send_test_signal` | setup | `POST /v1/setup/test-signal`: paper only |
| `venue_check` | setup | `POST /v1/setup/venue-check`: test accounts only |

Not offered at all: going live, back to paper, the emergency stop, close-all, closing or changing
positions, orders. While the relay runs live, or its deployment file says live, `setup_write`,
`setup_remove` and `restart_relay` refuse: a live relay is changed by its owner in the console. A
restart (from here or the console) is refused when the relay would not start again with the files
and variables as they are (`relay doctor` names why). On the approval page the owner can leave the
setup permission out. The full API key also works as the bearer (for agents configured by hand); the
read-only key gets the read tools. Wrong tokens count towards the address lockout. A request with
an `Origin` header other than the relay's own address is refused.

### 4.2 The console (`/console`)

- Tabs: Home (what is set up and what is next), Setup (exchange, limits, sources, notifications,
  test, go live), Connect (what to paste into TradingView, a script, a no-code tool or an AI
  agent), Positions (with a Close button per position), Signals, Events, Stop.
- Setup writes the config files; keys are never typed into it. It names each variable to add to
  `.env` and shows whether it is set.
- The Stop tab offers Close all, Emergency stop and Resume, after you type the deployment's name.
- The key is kept in the browser tab's session storage only. The page loads nothing from
  elsewhere (strict content security policy) and refreshes every 10 s.
- A read-only key can view everything; its Close and Stop actions are refused (403).

---

## 5. Current limitations

**Signals in**
- One deployment per relay. A signal for any other deployment is refused.
- Only market execution. `limit` and `maker_chase` are accepted by the contract, then skipped.
- Only USDT symbols reach the executors. USDC symbols are skipped, even on Hyperliquid (send
  `BTCUSDT` there).
- `meta` is not part of the contract; a signal carrying it is refused.
- Passphrase sources have no signature window: replay protection is `ttl_s`, `sent_at` and the
  `id` only.
- A request refused before it is a valid signal (auth, size, JSON, schema, plugin, rate, busy) is
  not in the signal log. Look in the relay's log.
- The rate limit is per source and kept in memory: a restart resets it.
- `relay test-signal` cannot test a passphrase (TradingView) source or a source plugin; the
  console's test signal checks everything after the sender's proof.
- `POST /v1/orders` takes plain symbols only and has no stop percentage.
- The API key is one key for everything (plus a read-only one). It is not scoped per source or
  deployment.

**Venues**
- Every CCXT exchange is a community connector. Only native Bybit has been run against a real
  (demo) account.
- Funding is not read on Bitget and KuCoin futures: CCXT needs a symbol per call there, and the
  relay reads the whole account's. Funding stays unknown (never 0).
- No trailing stop on Gate, MEXC, KuCoin futures, Hyperliquid, Bybit through CCXT, spot, or an
  exchange without a profile.
- `isolated_margin` cannot be set on Gate, HTX or MEXC; `leverage` cannot be set on MEXC. Set
  them on the exchange.
- Closes the exchange makes are unpriced on Gate, MEXC, KuCoin futures, HTX, BingX, spot, and
  exchanges without a profile. A daily loss limit then refuses new entries that day.
- PnL from fills includes closing fees, not opening fees. Fees paid in another coin are not
  subtracted.
- MEXC stops expire after 7 days and Gate spot stops after a day. The relay renews them, but only
  while it runs.
- Native Hyperliquid and maker-chase execution exist in the code but are not wired.
- The drawdown guardian is not wired. The daily loss limit stops new entries; it does not
  flatten.

**Notifications**
- Telegram, Discord and webhook only, plus plugins. No log-file sink.
- No chat commands. Nothing can be controlled from Telegram or Discord.

**Read APIs**
- `/metrics` counts Bybit call failures only.

**Running it**
- One relay per exchange account (or subaccount). Each relay tags its orders with its own
  deployment id, and a position tagged by a relay this database does not know is not taken over;
  but two relays on one account still see each other's positions in the account's totals.
- Stop the container with enough time: `compose.yaml` gives it 60 s (`stop_grace_period`), so an
  entry in flight finishes with its stop. Give other supervisors the same.
- Put a TLS reverse proxy in front before the relay is reached from outside this machine.
