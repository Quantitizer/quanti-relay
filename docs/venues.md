# Venues: where the relay trades

The relay trades USDT-settled linear perpetual futures (USDC-settled on Hyperliquid), or the spot
market (see [Spot](#spot)). It reaches exchanges in two ways:

- **Bybit, native.** The relay's own Bybit connector (`relay/exchange/bybit/`) and its live
  order feed. This is the connector the relay was built and tested on.
- **Any other exchange, through [CCXT](https://github.com/ccxt/ccxt).** CCXT is the open-source
  library that speaks to about a hundred exchanges in one common language. `relay/venue/ccxt_venue.py`
  answers the calls the relay makes in the same shape as Bybit, so the trading core is the same
  on every exchange. Install it with `pip install "quanti-relay[venues]"`.

## Choosing the exchange

In the deployment file:

```yaml
venue:
  exchange: okx          # bybit, or a CCXT exchange id
  environment: testnet   # REQUIRED for a CCXT exchange: demo, testnet or live
```

- For Bybit, leave `environment` empty. The host comes from `RELAY_BYBIT_DEMO` / `RELAY_TESTNET`,
  as before.
- For any other exchange there is no default environment, so live trading is never chosen by
  leaving a setting out.
- Paper trading (dry-run) works on every exchange without keys: the relay reads prices and market
  rules only.

Keys come from the relay's environment only. With Docker Compose, `.env` is how they get there;
without Docker, export them in the shell that starts the relay (it does not read `.env` itself).
Use keys that can trade but never withdraw.

| Exchange needs | Variables |
|---|---|
| native Bybit (perpetuals) | `BYBIT_API_KEY`, `BYBIT_API_SECRET` (no `RELAY_` prefix) |
| key and secret | `RELAY_<EXCHANGE>_API_KEY`, `RELAY_<EXCHANGE>_API_SECRET` |
| a passphrase as well (OKX, Bitget, KuCoin) | `RELAY_<EXCHANGE>_API_PASSPHRASE` |
| a wallet (Hyperliquid) | `RELAY_HYPERLIQUID_WALLET_ADDRESS`, `RELAY_HYPERLIQUID_PRIVATE_KEY` |
| other credentials CCXT names (an exchange without a profile) | `RELAY_<EXCHANGE>_API_UID`, `_API_LOGIN`, `_API_TOKEN` |

For example: `RELAY_OKX_API_KEY`. Then run `relay check-config`. It shows the exchange, the
environment, whether the keys are set, the connector's tier, and anything the exchange cannot
carry out.

## Spot

Set `market: spot` in the venue section to buy coins with USDT instead of trading perpetuals.
This works on any CCXT exchange with USDT spot markets, Bybit's spot included (through CCXT, so
it needs `environment`).

```yaml
venue:
  exchange: binance
  environment: testnet
  market: spot
  dedicated_account: true   # required: this account holds only what the relay buys
```

What is different on spot:
- **Long only.** A buy opens or adds; a sell only closes. A short signal, a flip to short and a
  target book with short legs are refused (`short_unsupported`) before anything is closed.
- **The position is the coins the account holds.**
  - Every coin held above dust (the market's minimum order) is taken for one of the relay's
    positions, and the relay manages and sells it. That is why spot needs `dedicated_account:
    true`: use an account or sub-account that holds nothing else. At start the relay lists
    the coins already there.
  - The entry price is the average cost of the most recent buys that make up the holding.
  - A fee charged in the coin makes the holding a little smaller than the order. The relay
    follows the holding.
- **The stop holds the coins.** It is a stop-market sell resting on the exchange, and the coins
  it would sell are not free for another order while it rests. So:
  - moving the stop cancels the old one first, then places the new one (on perpetuals it is the
    other way round). If the new one is refused, the old one is put back;
  - a sell takes the relay's stop away to free the coins, sells, and puts the stop back on
    what is left;
  - nothing else rests beside it: `take_profit_pct`, `take_profit_ladder` and
    `trailing_distance_pct` are refused at start, and a take-profit sent with a signal is not
    placed.
- **No leverage and no margin mode**: `leverage` and `isolated_margin` are refused at start.
- **No funding.** The funding history is empty, not unknown.
- **PnL.** Spot fills carry no realised PnL, so a close the exchange makes (a stop that fires)
  stays unpriced, as on the venues without PnL per fill.
- **Prices on spot market orders.** A closing sell never carries a price: Binance, BingX and
  Bybit read a price on a market order as "sell this much USDT", and a falling price would then
  need more coins than are held. A buy carries one only where the exchange needs it to work out
  what to spend (Bitget, Gate, HTX).
- **A fee taken in the coin itself** (Binance without BNB, OKX, Bybit) is subtracted from what a
  buy records, so the record matches what the account holds and the close sells it all.
- **Stops on spot are a separate question from stops on futures.** The relay asks CCXT whether
  the exchange's spot market takes a stop-market sell. Today, by CCXT's table, Binance, Bybit,
  OKX, Bitget, Gate, KuCoin, BingX, Kraken and Coinbase do. MEXC, HTX and Hyperliquid do not on
  spot; there the relay starts only with `require_stop: false`. HTX spot is not offered at all
  (CCXT would send a spot stop as an immediate market sell).
- Positions on spot are recorded under their own venue id (`binancespot`), so switching between
  an exchange's futures and its spot with live positions open is refused like any other switch.

## Tiers

- **Verified.** Run against the exchange's demo or test account with the verification steps below,
  and the results recorded. The relay stands behind these.
- **Community.** Any CCXT exchange with linear perpetuals and positions (about 54 of them in CCXT
  4.5.89). It works through the same connector and the same safety checks, but nobody has run it
  against that exchange yet. `relay check-config` and the start-up log say so.

**Today every CCXT exchange is a community connector.** Bybit native is the only one run against
a real (demo) account.

## The top 10

The top 10 is ranked by perpetual-futures volume and open interest, checked on 2026-11-03. Each
of these has a profile in `relay/venue/profiles.py` covering its client-id format, how its stop
orders are asked for, its test environment, its keys and its capabilities.

| Exchange | CCXT id | Test environment | Stop on the exchange | Trailing stop | Venue PnL per fill | Status |
|---|---|---|---|---|---|---|
| Binance USDⓈ-M | `binance` | demo, testnet | yes | yes | yes | community |
| Bybit | (native) | demo, testnet | yes | yes | yes | verified (native connector) |
| OKX | `okx` | testnet (OKX demo trading) | yes | yes | yes | community |
| Bitget | `bitget` | demo | yes | yes | yes | community |
| Gate | `gate` | testnet | yes | no | no | community |
| MEXC | `mexc` | none | yes (plan orders) | no | no | community (futures API reopened 2026-03-31) |
| KuCoin futures | `kucoinfutures` | none | yes | no | no | community |
| HTX | `htx` | none | yes | yes | no | community |
| BingX | `bingx` | testnet (VST demo) | yes | yes | no | community |
| Hyperliquid | `hyperliquid` | testnet | yes | no | yes | community (settles in USDC) |

"Venue PnL per fill" decides how a close the exchange made, such as a stop that fired, is priced:
- **Yes:** from the exchange's own fills.
- **No:** such a close stays unpriced. A daily loss limit then refuses new entries for that day,
  and `relay check-config` warns about it.

## How the connector behaves (what is the same everywhere)

- **The stop.** On Bybit the stop sits on the position itself. Elsewhere it is a reduce-only
  stop-market order resting on the exchange.
  - When the stop moves, the new order is placed *before* the old one is cancelled, so the
    position is never left without a stop.
  - A position reports its stop only when the order covers the whole position. If the position
    grew, the stop audit re-protects it.
  - If the resting orders cannot be read, the read fails instead of reporting "no stop", which
    would place a second one.
  - Your own orders on the exchange are read but never cancelled.
- **Quantities.** The relay counts in coins. Many exchanges count perpetuals in contracts (one OKX
  BTC contract is 0.01 BTC). Every quantity is converted with the market's contract size, in
  decimal arithmetic.
- **One-way mode.** At start the relay sets the account to one position per symbol. If it cannot,
  it warns, and you set it in the exchange's settings.
- **Refusals.** A setting the exchange cannot carry out stops the start rather than being skipped:
  - a trailing stop where there is none
  - a take-profit ladder without resting reduce-only limits
  - isolated margin or leverage where the API cannot set them
  - an exchange without stop orders while `require_stop` is on (see below)
- **Trading without a stop.** `require_stop: true` (the deployment file's default) refuses
  any entry without a stop. Set `require_stop: false` to trade without one: entries are then
  made without a stop on the exchange unless the signal sends one. On an exchange that cannot
  hold stop orders at all (CCXT knows of about a dozen, such as `xt`), the relay starts only
  with `require_stop: false`. There, a signal that asks for a stop (an entry, a flip, a book) is
  refused (`stop_unsupported`) before anything is closed, and nothing that needs a trigger order
  is sent: `take_profit_pct` and the break-even rules are refused at start, and a take-profit
  sent with a signal is not placed (on such an exchange a plain order would close at market).
- **Exchange quirks handled in profiles.**
  - MEXC stops are plan orders that need the trigger direction (falling for a long's stop).
    CCXT does not read MEXC's client ids or close-side codes, so the connector reads them from
    the raw answer. Contracts MEXC marks `apiAllowed: false` are not offered.
  - MEXC plan orders live 7 days at most. The relay renews a stop before that: past 80% of its
    life it reads as missing, and the stop audit places a new one before the old is cancelled.
  - MEXC's leverage and margin-mode calls need arguments CCXT does not fill in, so `leverage`
    and `isolated_margin` are refused there: set them on the exchange. Gate and HTX cannot set
    the margin mode through CCXT either, so `isolated_margin` is refused there too.
  - Bitget and KuCoin futures: CCXT reads funding payments one symbol at a time, so the relay
    leaves their funding unknown (never 0).
  - Symbols: signals use USDT names (`BTCUSDT`). On a USDC venue (Hyperliquid) send `BTCUSDT`
    too: the connector trades the USDC perpetual. A `BTC/USDC:USDC` signal is skipped.
  - Tags. The relay recognises its own stops by the tag it gives them. At the first stop on an
    exchange it checks that the exchange gives the tag back. If it does not, the stop is
    cancelled again and stops are refused there from then on, rather than piling up.
  - Hyperliquid market orders carry a reference price, from which Hyperliquid caps the slippage.
  - Where each exchange files its stop orders (read from CCXT 4.5.89's source, 2026-11-03):

    | Exchange | Stops are listed with | Notes |
    |---|---|---|
    | Binance futures | the trigger flag (algo orders) | spot: among the plain open orders |
    | OKX | `ordType: conditional`; trailing stops apart | the tag's echo on algo orders is checked at the first stop |
    | Bitget | `planType: profit_loss`; trailing stops apart | spot stops are asked for with `triggerPrice` |
    | Gate | the trigger flag | spot stops expire after a day and are renewed |
    | KuCoin futures | the trigger flag | a unified-mode account is untested |
    | HTX | stop, take-profit and trailing each in its own list | client ids are numbers; spot is not offered |
    | BingX | among the plain open orders | |
    | Bybit (spot through CCXT) | `orderFilter: tpslOrder` | |
    | Hyperliquid | among the plain open orders | |
    | MEXC | the trigger flag (plan orders) | 7-day lifetime, renewed |

    What the source cannot settle (whether an exchange echoes the tag, accepts a parameter, or
    lists a kind) is what `relay verify-venue` checks on a test account.
- **Switching exchanges.** The start is refused while live positions are open on the previous
  exchange or environment. Those positions would be invisible to the relay and could not be
  protected or closed.
- **Funding.** Funding is read where the exchange publishes it. Where it does not, it stays
  unknown, never 0.
- **PnL from the exchange's fills.** It is net of the closing fees. Fees for opening the position
  are not in it yet.

## Verifying an exchange (community -> verified)

This needs a demo or test account on the exchange, with its keys exported in your terminal (never
in a file or a chat).

1. Set the venue section to that exchange and its test environment, then run `relay check-config`.
   It should show the venue line, the keys set, and no PROBLEM lines.
2. Run the scripted check. The size and the stop distance are yours to choose; nothing is preset.

   `relay verify-venue --symbol <SYMBOL> --notional <USDT> --stop-pct <PERCENT>`

   Without `--confirm` it only says what it would do. With `--confirm` it trades one small
   position on that symbol and reports each step as PASS, FAIL or skip:

   | Step | What must hold |
   |---|---|
   | read | the market's rules, its price and the balance can be read; the size meets the minimum; the symbol holds no position yet (the check never touches one that is there) |
   | open | a market buy fills, is found again by the relay's tag, and shows as a long |
   | stop | a stop rests on the exchange and shows on the position |
   | move | after moving it, exactly one stop of the relay's rests, at the new price |
   | same | the same stop again is "not modified", and nothing new is placed |
   | ladder | a reduce-only limit for half the position rests and is cancelled (spot: a partial sell instead, after which the stop must be back on the rest) |
   | restart | a fresh connector finds the position and its stop again |
   | close | a reduce-only sell closes it, and nothing of the relay's rests |
   | records | the exchange's records of the close carry PnL per fill (skipped where it has none) |
   | funding | the funding history is read, or said to be unavailable |

   After a failed step the rest are skipped, and the check closes its position and removes its
   orders whatever happened. If even that fails, it says so in capitals, and you close the
   position on the exchange by hand.
3. It refuses `environment: live`. An exchange without a test environment (MEXC, KuCoin, HTX)
   cannot be verified this way and stays a community connector.
4. When every step passes, record the run (date, exchange, environment, the output), and set
   `verified=True` in that exchange's profile in the same change.

The relay never switches to live itself: `environment: live` and `dry_run: false` are the owner's
settings alone.

## Adding an exchange

An exchange CCXT does not reach can be added as a venue plugin, with no change to the relay
(docs/plugins/venues.md). For a CCXT exchange:

Any CCXT exchange with linear perpetuals already works as a community connector, with no code.
A profile in `relay/venue/profiles.py` is needed only when the exchange differs from CCXT's
unified behaviour:
- its client order id format
- extra parameters for stop orders, or how they are listed (`trigger_list_params`)
- its test environment
- a passphrase or wallet
- the field holding a fill's realised PnL
- whether its balance already includes unrealised PnL

The tests in `tests/test_ccxt_venue.py` run the connector, and the relay's own engine, against an
in-memory exchange (`tests/fake_ccxt.py`). A new behaviour gets a test there.

## The calls (for connector authors)

The connector answers these calls in Bybit v5's shape. The relay reads exactly these fields.

| Call | Fields read |
|---|---|
| `list_instruments(symbol=)` | `lotSizeFilter.qtyStep`, `minOrderQty`, `maxOrderQty` / `maxMktOrderQty`, `minNotionalValue`; `priceFilter.tickSize` (coins) |
| `get_positions(symbol= or settle_coin=)` | `symbol`, `side` (`Buy`/`Sell`), `size` (coins, positive), `avgPrice`, `markPrice`, `stopLoss` (`''` = none), `trailingStop`, `unrealisedPnl`, `createdTime` (ms) |
| `get_ticker(symbol)` | `lastPrice`, `markPrice` |
| `get_balance_safe()` | `(equity, 'wallet')`, or `(0.0, 'unavailable')`, never raises |
| `place_market_order(symbol, side, qty, reduce_only=, order_link_id=)` | `orderId` |
| `place_limit_order(..., price, post_only=, reduce_only=, order_link_id=)` | `orderId` |
| `get_order(symbol, order_id= / order_link_id=)` | `orderStatus` (`New`, `PartiallyFilled`, `Filled`, `Cancelled`, `PartiallyFilledCanceled`, `Rejected`, `Deactivated`), `cumExecQty`, `avgPrice`, `orderLinkId`; `None` if unknown |
| `get_order_history(...)` | as `get_order`, plus `side`, `reduceOnly` |
| `get_open_orders(symbol=)` | `orderId` |
| `amend_order`, `cancel_order` | raise on failure; an order already gone is "order not exists or too late" |
| `set_trading_stop(symbol, stop_loss= / take_profit= / trailing_stop=)` | returns `{retCode, retMsg}`: 0 OK, 34040 unchanged, other = refused; 0 removes |
| `set_leverage`, `switch_isolated_margin` | returns `{retCode}`: 0, 110043 / 110026 = unchanged |
| `get_closed_pnl(symbol, start_time)` | `orderId`, `side` (the closing order's), `closedSize`, `avgExitPrice`, `closedPnl` (`''` = not known), `updatedTime` |
| `get_transaction_log(start_time)` | `type` `SETTLEMENT`, `symbol`, `change`, `transactionTime`; raises when unavailable |

A refusal by the exchange raises an error whose text starts with `Venue rejected`. A lost answer
(network) raises any other error, and the relay then looks the order up by its tag before
resending.
