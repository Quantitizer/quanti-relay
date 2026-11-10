# A venue plugin, step by step

Most exchanges need no plugin. Any CCXT exchange with USDT perpetuals already works as a
community connector (docs/venues.md), and an exchange's quirks go in a profile in
`relay/venue/profiles.py`. Write a venue plugin only for an exchange CCXT does not reach, or
reaches too badly for a profile to fix.

A venue plugin builds the client the relay trades through. The relay's engine was built on
Bybit's interface and reads Bybit's answer fields, so the client answers the same calls in the
same shape. That way the engine, its safety checks and its test suite run unchanged on your
exchange. docs/venues.md ("The calls") lists every call and every field the relay reads.

## 1. Start from the template

Copy `relay/plugins/_template_venue.py`. It is a complete venue that keeps everything in memory:
orders fill at once, and the stop sits on the position. Keep each method's signature and its
answer's shape, and replace the body with calls to your exchange's API. The rules:

- **`build(environment, *, paper, environ)`** is the entry point.
  - `environment` is the deployment's `venue.environment`: demo, testnet or live.
  - Read keys from `environ`, named after your exchange (`RELAY_MYEXCHANGE_API_KEY`, ...).
  - On paper the relay only reads prices and market rules, so do not demand keys then.
- **`venue_id` is the name you register the plugin under** (2-30 lowercase letters and
  digits). Positions and the account are recorded under it, so the relay refuses a client that
  reports another name. Renaming it later hides the open positions from the relay.
- **Quantities are in coins, prices in USDT**, as plain decimal strings. Convert contracts
  yourself.
- **Errors.**
  - A refusal by the exchange raises an error whose text starts with `Venue rejected`.
  - An order that is already gone on cancel raises
    `relay.util.circuit_breaker.BenignOrderStateError`.
  - A lost answer (network) raises any other error. The relay then looks the order up by its tag
    before it sends again, so never let a lost answer look like a refusal.
- **The stop rests on the exchange.** It is either an order or on the position, as on Bybit.
  `get_positions` shows it in `stopLoss` only when it covers the whole position. If your
  exchange cannot hold one, set `can_stop = False`: the relay then starts only with
  `require_stop: false` and refuses every signal that asks for a stop.
- **Your own orders on the exchange are not the relay's.** The relay recognises its orders by the
  tag it sends as `order_link_id`. Give the tag back in `orderLinkId`.
- **Say what the venue can do.** `can_stop` and `can_short` are required (True or False). The
  others are False unless you set them, and the relay refuses the deployment settings that need
  them:
  - `can_ladder`: reduce-only limit orders rest (take-profit ladders);
  - `can_trail`: a trailing stop;
  - `can_set_leverage` and `can_set_margin_mode`: leverage and the margin mode set through the API;
  - `gives_pnl`: `get_closed_pnl` carries the realised PnL of a close.

## 2. Check it

```python
from relay.plugins.contract import check_venue

async def test_contract():
    await check_venue(build, environment="testnet", environ={...test-account keys...},
                      symbol="BTCUSDT", notional=..., stop_pct=...)
```

It checks that every call is there and that `venue_id` is usable. Then it runs the steps of
`relay verify-venue` against your client: open, stop, move, ladder, restart, close. The size and
the stop distance are yours to choose. On a real exchange it trades, so point it at a test
account only.

## 3. Register and use it

```toml
[project.entry-points."quanti_relay.venues"]
myexchange = "my_package.module:build"
```

```yaml
venue:
  exchange: myexchange
  environment: testnet
```

Then `relay check-config`, and `relay verify-venue --symbol <SYMBOL> --notional <USDT> --stop-pct
<PERCENT> --confirm` on the test account. A venue plugin is untested until that passes, and the
relay says so at every start.
