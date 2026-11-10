# Quanti Relay — Design Spec v0.2 (approved 2026-10-29)

## 1. What it is

A self-hosted service that **receives trading signals as JSON, checks them against configurable
risk rails, and executes them on exchanges**. It never generates signals itself.

Design goals, in priority order:

1. **Safe by default.** A fresh install trades nothing real: dry-run and testnet are on, every
   position needs a stop, and a kill switch exists from the first boot.
2. **Many ways in, many ways out.** Inputs, venues and notification outputs are plugins, each
   following a written contract. Adding one should take an afternoon.
3. **Readable by humans and AI assistants.** Small modules, typed interfaces, a JSON Schema for every
   message, runnable examples, and `AGENTS.md` instructions an AI coding tool can follow.
4. **One command to run.** `docker compose up`.

Non-goals: signal generation, indicators, charting, testing strategies on history, market-data
warehousing.

## 2. Architecture

```
  INPUTS (source plugins)          CORE                                      VENUES (venue plugins)
  ───────────────────────          ────                                      ──────────────────────
  generic webhook (HMAC)  ─┐                                               ┌─ Bybit (native)
  TradingView alert       ─┼─▶ authenticate ─▶ normalise ─▶ dedupe/expiry ─┤  CCXT perpetuals and spot
  REST API (API key)      ─┤        │                            │         └─ venue plugins
  Quantitiser             ─┘        ▼                            ▼
                               signal log  ─▶ RISK RAILS ─▶ ROUTER ─▶ EXECUTOR ─▶ VENUE ADAPTER
                               audit log      accept/clip/   deployment   event: entry/exit
                                              reject         → account    target: diff → orders
                                                                │
  OUTPUTS (sink plugins)  ◀──────────── event bus ◀─────────────┘
  Telegram · Discord · webhook-out · sink plugins

  BACKGROUND LOOPS: position/fill reconciliation · stop-loss audit · stop rules · size-drift repair · PnL audit
```

What the owner needs to know becomes an event (the list is in `relay/events.py`: a signal's
outcome, positions opened and closed, a stop that could not be set, a position without a stop,
size drift, the emergency stop). Events are stored, listed at `GET /v1/events` and delivered by the
sinks. `docs/connectors.md` has the full list.

## 3. Signal contract v1

One envelope and two kinds of signal. The JSON Schema in `schemas/signal.v1.json` is authoritative,
and this section describes it.

### Envelope (all signals)

| Field | Req | Meaning |
|---|---|---|
| `version` | yes | `"1"` |
| `id` | yes | Sender-unique idempotency key. A repeat is acknowledged and never executed twice. |
| `source` | yes | Registered source name. It selects the auth method and the source's rails. |
| `deployment` | yes | The deployment this signal targets: a named account plus rails config. |
| `sent_at` | yes | ISO-8601 UTC. |
| `ttl_s` | no | The signal is rejected as stale after `sent_at + ttl_s`. Default 60. |
| `kind` | yes | `"event"` or `"target"`. |
| `dry_run` | no | Can only make a signal *more* cautious. It can never override a deployment that is in dry-run. |

### `kind: "event"` — discrete actions (TradingView-style)

| Field | Meaning |
|---|---|
| `action` | `open` · `close` · `reduce` · `flip` · `close_all` · `move_stop` (needs `stop`; refused if the price has already passed it) |
| `symbol` | Canonical symbol in CCXT unified format, e.g. `BTC/USDT:USDT` (linear perp). Source adapters also accept common aliases (`BTCUSDT`, `BTCUSDT.P`, `BYBIT:BTCUSDT.P`) and normalise them. |
| `side` | `long` / `short`. Required for `open` and `flip`. |
| `size` | One of: `{"notional_usd": <USDT>}`, `{"equity_pct": <PERCENT>}`, `{"risk_pct": <PERCENT>}` (needs `stop`), `{"qty": <COINS>}`, `{"close_pct": <PERCENT>}` for `reduce`. |
| `stop` | `{"price": <PRICE>}` or `{"pct": <PERCENT>}`. **Mandatory for `open` unless the deployment explicitly disables it.** |
| `take_profit` | Optional list of `{"price" \| "pct", "close_pct"}`: a take-profit ladder. Each level closes its `close_pct` of the position at its price (or `pct` from entry). Live, each level rests on the venue as a reduce-only limit order; paper levels fill when the price reaches them. Levels may close less than 100% in total; the rest stays with the stop. While an earlier position's levels may still rest on the venue, a live entry on that symbol is refused (`take_profit_leftover`) until they are cleaned up. |
| `trailing` | Optional `{"distance_pct": <PERCENT>, "activate_pct": <PERCENT>}`: a trailing stop that follows the best price at `distance_pct`, starting once the position is `activate_pct` in profit (omitted: at once). It runs beside the fixed stop, which stays in place. No default: without it (and without the deployment's own trailing settings) there is none. |
| `break_even` | Optional `{"trigger_pct": <PERCENT>}`, `{"trigger_r": <MULTIPLE>}` or `{"trigger_tp": <LEVEL>}` (after that take-profit level fills), plus optional `"buffer_pct"`: once the position is that far in profit (a %, or a multiple of its initial risk from entry to the opening stop), the relay moves its stop to the entry price plus the buffer. It only ever tightens. No default, as above. |
| (off switch) | `"take_profit": false`, `"trailing": false` or `"break_even": false` turns that rule off for this order when the deployment has its own. |
| `execution` | `market` · `limit` (`price`) · `maker_chase` (`window_s`) in the contract; only `market` (the default) is carried out so far - the others are refused with `execution_not_supported`. |

### `kind: "target"` — a whole book (portfolio-style)

| Field | Meaning |
|---|---|
| `weights` | `{"BTC/USDT:USDT": <WEIGHT>, "ETH/USDT:USDT": -<WEIGHT>, …}`. Signed fractions of deployment equity. Symbols left out go to zero. |
| `gross_leverage` | Optional scalar applied to the weights before the rails. |
| `stop_pct` | Native stop attached to every leg. |
| `execution` | Same as for events: market for now (`maker_chase` is refused until the maker executor is wired). |

The relay diffs the target against the live book (`relay/target_book.py`,
`relay/signals/target_executor.py`):

- Target value per symbol = weight x account equity x `gross_leverage`, valued at the mark
  price. The rails apply to the whole book: symbols off the lists dropped, each leg clipped to
  `max_notional`, the book scaled (shape kept) to `max_gross_leverage`. With `target.neutralize`,
  the larger side is trimmed so long value equals short value. Legs below the venue minimum are
  dropped (with `neutralize`, re-checked until both hold). The engine applies its own checks again
  on each order (its gross-leverage rail counts open positions at their entry value, and the
  position-count and exposure limits are not planned for: a leg they refuse is reported).
- Actions: enter, exit (symbols left out go to zero), resize up or down, flip, or hold (a
  same-side change under the deployment's `target.min_change_pct` of equity, or too small for the
  venue to execute).
- Nothing is traded on a wrong picture: a close in progress, two positions on one symbol, a live
  venue position that is not exactly the book's (also on target symbols the book does not hold
  yet), or another live record on a target symbol skips the signal. With `require_stop`, a book
  without `stop_pct` is refused.
- Execution, margin first: exits, reductions, flips, increases, entries, each through the
  engine's checks. A failed close or reduction stops the new exposure (a flip then only closes).
  New legs carry `stop_pct` only (no default take-profit, trailing or break-even); resized
  positions keep their stop. A position with a take-profit ladder, break-even or trailing stop is
  not added to (those rules were set for its old size).
- `execution`: market only for now (maker_chase is refused until the maker executor is wired).

A target book manages the whole book of its deployment: give it a deployment no event source
trades. This is the predecessor's rebalance pipeline without its strategy and its venue-specific
compliance rules.

### Response

Signals are sent to `POST /v1/signals`. The answer is synchronous, the execution is not:

- `202` `{"id", "signal", "status": "accepted", "reasons": []}`: carried out in the background. The
  outcome (`executed`, `skipped` with the reason, or `failed`) is read at `GET /v1/signals/{signal}`
  (with an API key), and through the sinks.
- `202` with `"status": "duplicate"`: this `id` was received before with the same signal (a retry;
  `sent_at` may differ) and is never carried out twice.
- `409` with `"status": "conflict"`: this `id` was used before for a different signal. It is not
  carried out, and a `signal.conflict` event says so. Give every signal its own id.
- `422` with `"status": "rejected"`: not a valid v1 signal (nothing is logged), or stale, sent in
  the future or for an unknown deployment; `403`: a deployment the source is not bound to. These
  last ones are logged under their `id`, so a corrected signal needs a new `id`.
- `401` (unknown source, bad or old signature: one answer for both, so source names cannot be
  probed), `403` (source disabled), `400` (not JSON) and `413` (over 64 KiB, refused before it is
  read) are answered before the signal is read, and nothing is logged.
- `429`: the source sent more than its `max_signals_per_minute` (counted only for correctly
  signed signals). Not logged: send it again later.
- `503` "busy": 64 signals are being carried out already. Not logged: send it again.
- A signal cut off by a shutdown or restart is marked `failed` ("interrupted"), never left pending.

### Size drift (records vs venue)

After every position sync the relay compares each live position's recorded size with the venue's.
Take-profit fills the ladder has not recorded yet (a level partly filled and still resting) are
counted first, so they are never mistaken for drift. A difference (a manual reduce or add on the
venue, an order the relay could not record) raises an alert (`position.drift_detected`). If a
later sync finds the same difference, at least two stop-rules intervals after it was first seen,
the record is repaired (`position.drift_repaired`): less on the venue becomes a partial close
priced from the venue's closed-PnL records of the orders no record accounts for; more on the
venue becomes the venue's average entry, with the difference recorded as an entry. A venue position
shared with other records is never repaired (`position.drift_unrepairable`). Events are stored
(`GET /v1/events`) and delivered by the notification sinks.

## 4. Authentication (per source)

| Method | For | How |
|---|---|---|
| HMAC-SHA256 | own systems, Quantitiser | Header `X-Relay-Signature: t=<unix>,v1=<hex>` over `t + "." + body`. Rejected outside ±30s (replay protection). The same signature over `t + ".GET " + path` reads the owner's limits (`GET /v1/sources/<name>/limits`, docs/connectors.md §1.6). |
| Body passphrase + IP allowlist | TradingView, which cannot sign | `"passphrase"` field in the body, compared in constant time and removed before the signal is read or stored. The source is locked to the addresses in its `allow_ips` (required; for TradingView, the ones it publishes - not shipped with the relay, as they change). `client_ip_header` names the header a reverse proxy you run puts the client address in; it is believed only on requests from `trusted_proxies` (required with it) and read from the right, so a forged entry the client adds on the left counts for nothing. |
| API key | scripts, dashboards, the console | `Authorization: Bearer <key>` or `X-API-Key: <key>`. Two keys: `RELAY_API_KEY` may do everything, `RELAY_API_READONLY_KEY` (optional) may only read. |

Each source is a file `config/sources/<name>.yaml` (folder: `RELAY_CONFIG_DIR`):

```yaml
auth: hmac
secret_env: RELAY_SOURCE_MYBOT_SECRET   # the variable holding the secret (at least 32 characters)
deployments: [default]
enabled: true
```

`max_signals_per_minute` (optional) limits how fast a source may send. `relay check-config`
reports what the relay would run with, and `relay test-signal --source <name> ...` sends a signed
signal that is always `dry_run: true` to a running relay.

The secret itself is never in a file. Give every relay its own secret: a signature does not name
the relay, so a request signed for one relay is valid at another that shares the secret. A source file that is invalid, or whose secret is missing,
stops the relay at start.

**Reachable from the internet** (`relay/api/guard.py`, settings in `.env.example`):

- On any address but localhost the relay refuses to start without `RELAY_API_KEY`, or with an API
  key shorter than 32 characters.
- An address that sends `RELAY_AUTH_MAX_FAILURES` wrong keys within `RELAY_AUTH_FAILURE_WINDOW_S`
  is refused (429) for `RELAY_AUTH_LOCKOUT_S`, even with the right key, and an `auth.locked`
  event says so. IPv6 addresses count per /64. Only the full key clears the count (the read-only
  key and a chat app's token do not). The emergency stop and close-all still take the full key
  from a locked address, so a stranger sharing it cannot keep the owner from stopping. Signal
  intake is not part of this: a source's secret is long and checked by the source's own rules.
- `RELAY_API_ALLOW_IPS` (optional) closes the API, the console and the API documentation to every
  other address; signal intake and `/health` stay open.
- Behind a TLS proxy, `RELAY_TRUSTED_PROXIES` names it, and the client is the right-most entry of
  `RELAY_CLIENT_IP_HEADER` (every line of it) that is not a proxy. Without it every client looks
  like the proxy, and one wrong-key flood would lock everyone out. The shipped install trusts
  the TLS proxy's own fixed address only, and serves 80 and 443 over IPv4.
- Bodies over `RELAY_MAX_BODY_BYTES` are refused (413), and one that has not arrived within 15
  seconds is cut off (408); more than `RELAY_MAX_CONNECTIONS` connections at once are answered
  503. The shipped TLS proxy reads a request whole, with its own time limits, before it reaches
  the relay, so slow senders cannot fill the relay's connections.
- `/health` answers 503 `stuck` when a background loop (position sync, stop audit, stop rules)
  ended, or has not finished a round without an error for twice its interval plus ten minutes,
  and a `relay.stuck` event goes to the sinks. It says only its state; the version and the
  reasons are in `/v1/status`. `RELAY_HEARTBEAT_URL` (optional) is called every
  `RELAY_HEARTBEAT_INTERVAL_S` while `/health` is ok: a monitoring service of the owner's choice
  alerts when the calls stop, which covers a relay that is down or a server that is gone.
- The MCP endpoint for chat apps (`/mcp`, off unless `RELAY_MCP_ENABLED`) signs apps in with
  OAuth 2.1 and PKCE on the relay's own approval page, where the owner types the API key; tokens
  are stored as hashes and end when the app is removed. Its tools call the API routes in process,
  and leave out going live, the emergency stop, close-all and orders. While the relay runs live,
  or its deployment file says live, they change nothing (`docs/connectors.md` §4.3).
- Before a schema upgrade the SQLite database is copied to `backups/` beside it (the newest five
  kept, and the oldest of each schema version); `relay db backup` makes a copy on demand. Postgres is backed up with `pg_dump`.

Every source can only reach the deployments it is bound to. There are no unauthenticated endpoints
apart from `/health`, the API documentation page (`/docs`, `/openapi.json`) and the console's own
files (`/console`), which hold no data: every call needs a key. Source secrets are never stored at
all (only the name of the variable that holds one). Exchange keys come from environment variables
only and are never stored in the database or logged.

## 5. Risk rails (configuration, not code)

Built so far (`relay/rails.py`, under `rails:` in the deployment file): symbol allow/deny lists,
trading hours, max notional per position (USD or % of equity), max gross leverage over the whole
book (clip or reject, `on_limit`; open positions count at their entry value), and, under isolated
margin, the stop before the bankruptcy price for the leverage. A key set twice in a config file,
or open positions under a different deployment name, stop the start. The
position count, exposure, daily loss, cooldown, stop distance and mandatory stop live under `risk:`.

Templates with every setting explained and every number left empty: `examples/config/` (copy to
`config/`, which git ignores). No preset files ship, prop-firm ones included: every number is the
user's own.

Rails are defined per deployment in YAML (`config/deployments/*.yaml`; the format is documented
in `relay/deployment_config.py`, and one deployment is supported so far). The deployment trades on
paper when either its file or `RELAY_DRY_RUN` says dry-run, so going live takes both. `enabled:
false` stops new entries while closes, stops and the emergency stop keep working. Without a file,
the `RELAY_*` environment settings apply. Every rail returns one of
**accept**, **clip** (shrink the order to fit) or **reject**, together with a reason.

| Rail | Example |
|---|---|
| Kill switch (deployment / source) | `enabled: false` |
| Dry-run | default `true` |
| Max gross leverage over the book | `max_gross_leverage: <MULTIPLE>` |
| Max position notional, as % or USD | `max_notional: <USDT>` |
| Max open positions, max exposure | |
| Daily loss limit | new entries refused for the rest of the day; it does not close positions |
| Mandatory stop, and stop inside the liquidation price | `require_stop` (default on) |
| Stop distance (min / max) | |
| Symbol allow/deny list | |
| Signal rate limit (per source) and cooldown (per symbol) | |
| Trading-hours window | |
| Time stop (every position) | `position_management.max_hold_hours: <HOURS>`: a position open this long is closed at market, paper or live; counted from when the relay opened or imported it. A failed close is tried again on every pass and reported once |

Not built: an equity-dependent leverage schedule, a drawdown halt that flattens, a minimum venue
liquidity. No preset files ship.

## 6. Plugin interfaces

There are three Python protocols in `relay/plugins/`. Plugins register through package entry points
(`quanti_relay.sources`, `quanti_relay.sinks`, `quanti_relay.venues`), so third-party packages
are installed with `pip install` and nothing in the repository has to be edited. A plugin is
loaded only when a config file names it, and never in place of a built-in name.

- **`SourcePlugin`**: `name`, `translate(payload) -> v1 signal body`. It translates a vendor
  payload into the v1 contract and is named by `format:` in a source file. It does not
  authenticate: the relay checks the sender (hmac or passphrase) before the plugin sees the
  payload, so a plugin cannot weaken that check. (Decided 2026-11-03, instead of the
  `authenticate(request)` first planned.) A sender whose JSON carries no `source` posts to
  `POST /v1/sources/{name}/signals`.
- **`VenuePlugin`**: `build(environment, *, paper, environ) -> client`, where the client answers
  the calls listed in `docs/venues.md` ("The calls") in their shape, plus `venue_id`,
  `can_stop`, `can_short` and `prepare()`. It is named by `venue.exchange` when that is neither
  `bybit` nor a CCXT exchange id. This generalises the CCXT connector's own interface rather than
  a new one, so the engine runs unchanged.
- **`SinkPlugin`**: `from_config(name, settings, environ)`, `send(http, event)` (raising
  `SendError`) and `redact(text)`. It is named by `type:` in a sink file.

Each type ships with a template (`relay/plugins/_template_*.py`), a contract check
(`relay/plugins/contract.py`) that a new plugin must pass, and a step-by-step guide in
`docs/plugins/`.

## 7. Storage and runtime

- **SQLite by default** (a single file, nothing to install). **Postgres is optional** and selected by
  `RELAY_DATABASE_URL`. All persistence goes through dialect-neutral SQLAlchemy, and the test suite runs on
  both databases, from one baseline migration onwards.
- FastAPI service, a console the relay serves itself at `/console` (decided 2026-11-03 instead of
  a separate Next.js app: no build step, no outside scripts, one container). It sets the relay up
  without editing files (decided 2026-11-04): it writes the same config files, after the same
  checks, keeping each replaced version; keys stay in the environment, and it names the variable;
  going live from it takes the gates in `relay/setup.py` plus `RELAY_DRY_RUN=false`. It also shows
  positions, signals and events, and holds close-all, the emergency stop and resume.
  and the CLI `relay` (`serve`, `db upgrade`, `check-config`, `test-signal`, `verify-venue`).

## 8. Open-source hygiene gates (before the repo goes public)

1. `gitleaks` is clean over the full history, enforced in CI and as a pre-commit hook.
2. No names or identifiers of the project the relay grew out of, no server addresses, emails or
   account IDs (`scripts/hygiene_check.sh --release`, with the owner's private list).
3. No strategy parameters or strategy code.
4. All tests pass on a fresh clone with no secrets present.
5. `SECURITY.md` (how to report vulnerabilities), `CONTRIBUTING.md` and `AGENTS.md` exist.

## 9. Build phases

| Phase | Deliverable |
|---|---|
| R0 | Spec approved (this document) |
| R1 | The execution core, tests green, Bybit testnet dry-run works through the REST API |
| R2 | Signal contract v1, generic webhook and HMAC source, event executor, rails engine with YAML config |
| R3 | `target` executor (whole target books), Quantitiser source |
| R4 | TradingView source; Telegram, Discord and webhook-out sinks |
| R5 | CCXT generic venue (any CCXT exchange with linear perpetuals; profiles for the top 10, see `docs/venues.md`), spot mode (`venue.market: spot`: long only, dedicated account), plugin templates and contract tests, `AGENTS.md`, docs |
| R6 | Slim console, `docker compose up` quickstart, hygiene gates |
| R7 | AI-native self-setup (see below), then the manual test round, then public release |

### R7: the user's own AI sets the relay up in the user's own cloud

The project never hosts a relay and never touches a user's server, cloud account, keys or trades.
What it ships is the relay plus a predefined, tested path that any user's AI agent (one that runs
commands, or a chat app) can follow from "set it up for me" to a relay running on paper in a cloud
account the user owns, with any leading provider.

**What a cloud must offer** (any provider that does is fine): an always-on Linux server or
container with a persistent disk, a public address that stays the same (exchange keys are locked
to it), inbound port 443 for HTTPS, and somewhere to keep secrets out of files the AI sees.
`docs/cloud.md` lists recommended providers with the reasons; it is a suggestion, never a limit.

**What stays with the human**, guided and checked by the AI: the cloud account and its payment,
the exchange account and its identity checks, the exchange API key, the Telegram bot and the
TradingView alert, and going live. Secrets go from the human into the server's secret store
directly, never through the chat.

| Step | Deliverable |
|---|---|
| R7.1 | Internet-facing hardening: an address that sends too many wrong keys is locked out for a while, an optional address allowlist for the API and console, request size and connection limits, long keys required on a reachable address, the client address read through a TLS proxy you trust, a database copy before every upgrade, `/health` that notices stuck background work, a heartbeat for an outside monitor |
| R7.2 | Agent tools: `relay doctor`, non-interactive setup commands with JSON answers that are safe to repeat, secret entry that bypasses the chat, an exchange-key check (it can trade, it cannot withdraw, it is locked to the server's address) |
| R7.3 | One universal server setup script (cloud-init user data or run over SSH): the container, a TLS proxy in front with a free address by default, own domain optional, backups, updates |
| R7.4 | Provider recipes for the one step that differs ("create the server") and `docs/cloud.md`; at least two tested end to end |
| R7.5 | The setup playbook for AI agents: what to ask, run and check, and when to stop and hand over to the human |
| R7.6 | The relay's own MCP endpoint for chat apps, behind a standard sign-in, with tools limited to what the setup and daily use need; going live stays human |
| R7.7 | Review rounds, then a rehearsal: a fresh user and a fresh AI, from nothing to a relay on paper in their own cloud |

State (2026-11-04): R7.1 to R7.6 are built and tested (the install script against stand-ins for
the system tools). Still open, and needing the owner's own accounts: the rehearsal (R7.7), with it
the first two providers marked "rehearsed" in `docs/cloud.md` (R7.4), and whether TradingView takes
an IP-address URL (else `--sslip` or a domain is the default).

## 10. Decisions (2026-10-29)

1. **Symbols:** CCXT unified format is canonical, and inputs accept forgiving aliases.
2. **Storage:** SQLite is the default and Postgres is optional, through dialect-neutral SQLAlchemy.
3. **v1 sinks:** Telegram, Discord and webhook-out, all notifications only (built in R4:
   `relay/sinks.py`, `config/sinks/*.yaml`, examples in `examples/config/sinks/`; each sink filters
   by severity and kind and keeps its own place in the event list, so one that is down catches up
   later without holding the others back; while a receiver is down or busy its events wait, with
   growing pauses or its own Retry-After, and are never dropped; only an event it refuses
   outright is skipped, after five refusals). Controlling the relay
   through chat commands (`/halt`, `/status`) is deferred; when it comes, it is locked to allowed
   chats and treated as a security feature.
4. **Launch (2026-11-04):** the relay is self-hosted only. Each user's own AI agent sets it up in
   the user's own cloud, with any leading provider; the project publishes recommended providers
   as a suggestion, not a limit, and never hosts relays or holds user keys. Chat apps are
   supported through the relay's own MCP endpoint, not one the project runs. The relay gets a
   free HTTPS address by default; an own domain is optional.
