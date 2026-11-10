# AGENTS.md

These are instructions for AI coding agents, and for anyone else changing this repository.

## What this is

Quanti Relay is a self-hosted relay that takes trading signals as JSON and executes them on crypto
exchanges, behind risk rails the owner configures. It handles real money, so correctness and
safety come before features. `docs/SPEC.md` is the specification, and `docs/venues.md` and
`docs/plugins/` describe the exchanges and the plugins.

## Layout

| Path | What is there |
|---|---|
| `relay/api/` | the HTTP API (FastAPI): signal intake, status, positions, the emergency stop |
| `relay/signals/` | the v1 signal contract, sources (who may send) and the executors for event and target signals |
| `relay/execution/` | the engine: entries, exits, stops, take-profit ladders, the position sync, PnL |
| `relay/exchange/bybit/` | the native Bybit connector |
| `relay/venue/` | everything else: the CCXT connector (`ccxt_venue.py`, `ccxt_spot.py`), exchange profiles, `relay verify-venue` |
| `relay/plugins/` | plugin protocols, templates and contract checks |
| `relay/sinks.py`, `relay/events.py` | the event log and notification sinks |
| `relay/api/guard.py`, `oauth.py`, `mcp.py` | facing the internet: lockout and allowlist, the chat-app sign-in, the MCP endpoint |
| `relay/doctor.py`, `relay/envfile.py` | `relay doctor`; `relay secret` (standard library only, runs without the relay) |
| `deploy/` | the server install (`install.sh`, cloud-init `user-data.sh`, the `relay` wrapper, Caddy) |
| `relay/deployment_config.py` | the deployment file (trading settings, rails, venue) |
| `tests/` | pytest; `tests/fake_ccxt.py` and the fakes in `tests/test_engine.py` stand in for exchanges |

## Commands

- `uv sync` installs everything, the dev tools included.
- `uv run pytest -q` runs the whole suite on SQLite. It must pass before any commit.
- `RELAY_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest -q` runs the suite on Postgres
  as well. Run both for any change that touches the database.
- `uv run ruff check relay tests` must be clean.
- `relay doctor --json` checks a setup and says what to fix; `relay setup show|write|remove` writes
  the config files with JSON answers; `relay secret set NAME` takes a secret at the terminal, never
  through an agent. Setting a relay up for someone: `docs/setup-playbook.md`.
- `scripts/hygiene_check.sh` is the leak check (secrets, a private denylist, strategy residue,
  addresses). It runs as a pre-commit hook. Never weaken it: rename what it flags.

## Rules that are not negotiable

- **No secrets anywhere.** Keys, tokens and passphrases come from environment variables only,
  never from a file in the repository, a config file, a test fixture or a log line.
- **Never switch anything to live trading.** Do not set `dry_run: false`, `environment: live` or
  `enabled: true` on anyone's behalf, and do not run anything against a live account. Use demo or
  test accounts, and keep paper as the default.
- **No preset trading numbers.** Sizes, stops, leverage, limits and thresholds are empty inputs
  that the owner fills in, and empty means the rule is off. That applies to examples, defaults,
  templates and documentation too (write `<USDT>`, not a number).
- **Fail closed.** If something cannot be read (a stop order, a balance, a position), the answer
  is "unknown" or a refusal, never a default that looks fine. A position must never be left
  without its stop because a read failed.
- **Exchange behaviour is read, not assumed.** CCXT's unified parameters behave differently per
  exchange: where stop orders are listed, how they are cancelled, whether a price on a market
  order means USDT. Read the installed CCXT source for the exchange
  (`.venv/lib/python3.11/site-packages/ccxt/async_support/<exchange>.py`) before relying on a
  behaviour, and put the difference in the exchange's profile.
- **Library versions** must be at least two weeks old. CCXT is pinned exactly.

## How changes are made here

- **Every behaviour change gets a test that fails without it.** Check that it does by breaking the
  code on purpose (a mutation) and watching a test catch it. A test that still passes against
  broken code is not a test.
- **Read the real call paths.** The engine reads Bybit-shaped answers (`docs/venues.md`, "The
  calls"). A connector change is only right if the engine's use of it is right, so trace it.
- **Write plain English** in code comments, docs and messages: short sentences saying what happens
  and why.
- **Match the surrounding code's style and comment density.**
