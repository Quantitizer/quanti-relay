# Contributing

Thank you for helping. The relay handles real money, so changes are held to a careful standard.
Read [AGENTS.md](AGENTS.md) first: it lists the layout, the commands and the rules, and applies
to people as much as to coding agents.

## Before you open a pull request

1. `uv sync` and `uv run pytest -q`: the whole suite passes. If you touched the database,
   also run it on Postgres (`RELAY_TEST_DATABASE_URL=postgresql+asyncpg://...`).
2. `uv run ruff check relay tests` is clean.
3. `scripts/hygiene_check.sh` passes (install the hook with `git config core.hooksPath .githooks`).
4. **Every behaviour change has a test that fails without it.** Break your change on purpose and
   check that a test catches it.
5. No secrets, no preset trading numbers (sizes, stops, leverage, limits stay empty inputs), and
   nothing that switches to live trading.

## Good first contributions

- **A new exchange.** Most only need a profile in `relay/venue/profiles.py` and a run of
  `relay verify-venue` on its test account. Post the output in the pull request.
- **A plugin.** Sources, sinks and venues are plugins (`docs/plugins/`). They live in their own
  package and need no change here.
- **Documentation.** Fix anything that was unclear when you set the relay up.

## Reporting bugs

Open an issue with what you did, what you expected and what happened, plus the relevant log lines
with keys and secrets removed. Report security problems privately instead (see
[SECURITY.md](SECURITY.md)).

By contributing you agree that your contribution is licensed under the Apache License 2.0.
