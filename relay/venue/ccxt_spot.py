"""Spot trading through CCXT: buying a coin with USDT and selling it again, behind the same calls
as the perpetual connector (relay.venue.ccxt_venue), so the relay's engine is unchanged.

Spot is not a futures position, and the differences are handled here:
- Long only. A buy opens or adds; a sell only closes (an opening sell, a short, is refused).
- A position is the coins the account holds. Every coin held above dust (the market's minimum
  order) is a position, which is why spot needs an account or sub-account that holds only what
  the relay buys (the deployment's venue.dedicated_account). Its entry price is the average cost
  of the most recent buys that make up the holding (from the account's fills; empty when they
  cannot be read).
- The stop is a stop-market sell order resting on the exchange, and it holds the coins: they are
  not free for another order while it rests. So:
  - moving the stop cancels the old one before placing the new one (the other way round, as on
    perpetuals, would need the coins twice), and puts the old one back if the new one is refused;
  - a sell first takes the relay's resting stop away to free the coins, then sells, then puts the
    stop back on what is left (or straight back if the sell is refused);
  - no take-profit order, ladder or trailing stop rests beside it (the deployment's settings for
    them are refused at start; a take-profit sent with a signal is not placed).
- No leverage, no margin mode, no funding (the funding history is empty, not unknown).
- Equity is the USDT held plus the value of the coins held.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

from loguru import logger

from relay.util.circuit_breaker import BenignOrderStateError
from relay.venue.ccxt_venue import (
    _STOP,
    _TAKE_PROFIT,
    _TRAILING,
    REJECTED,
    CcxtVenue,
    VenueRejected,
    _f,
    _num,
    _tag,
    _tag_kind,
)
from relay.venue.profiles import Profile

_TRADES_LOOKBACK = 100                                       # fills read for a holding's average cost


class CcxtSpotVenue(CcxtVenue):
    spot = True
    can_short = False

    @property
    def spot_features(self) -> dict:
        """CCXT's table of what this exchange's spot market takes."""
        return (getattr(self.x, "features", None) or {}).get("spot") or {}

    @property
    def can_stop(self) -> bool:
        from relay.venue.factory import spot_stop_supported
        return spot_stop_supported(self.spot_features) and self._tags_echoed is not False

    def _price_param(self, kind: str, long: bool, price) -> dict:
        return {self.profile.spot_stop_param: float(price)}  # CCXT's spot stop-market sell

    def __init__(self, exchange, profile: Profile):
        super().__init__(exchange, profile)
        self.venue_id = f"{profile.exchange}spot"
        self._trigger_params = dict(profile.spot_trigger_list_params)
        self._listings = tuple(profile.spot_trigger_listings or (self._trigger_params,))
        self._trigger_extra = dict(profile.spot_trigger_params)
        self._max_age = profile.spot_trigger_max_age_s
        self._closing = {}                                   # a spot sell is closing by nature

    # ----------------------------------------------------------------- markets
    async def _load(self) -> dict[str, dict]:
        if self._markets is not None:
            return self._markets
        async with self._load_lock:
            if self._markets is None:
                markets = await self._call("load_markets")
                quote = self.profile.spot_quote
                found: dict[str, dict] = {}
                for m in (markets or {}).values():
                    if not (m.get("spot") and m.get("quote") == quote and m.get("active") is not False):
                        continue
                    found[f"{m['base']}USDT"] = m
                    self._relay_symbol[m["symbol"]] = f"{m['base']}USDT"
                self._markets = found
        return self._markets

    async def _market(self, symbol: str) -> dict:
        m = (await self._load()).get(str(symbol).upper())
        if m is None:
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): invalid symbol: no {self.profile.spot_quote} spot "
                                f"market for {symbol}")
        return m

    def _floor(self, m: dict, qty: Decimal) -> Decimal:
        step = self._step((m.get("precision") or {}).get("amount"))
        return (qty / step).to_integral_value(ROUND_DOWN) * step if step else qty

    def _dust(self, m: dict, qty: Decimal, price: float) -> bool:
        """Too little to sell: below the market's minimum amount or minimum order value."""
        limits = m.get("limits") or {}
        min_amount = _f((limits.get("amount") or {}).get("min"))
        min_cost = _f((limits.get("cost") or {}).get("min"))
        return qty <= 0 or float(qty) < min_amount or (price > 0 and float(qty) * price < min_cost)

    # ----------------------------------------------------------------- holdings as positions
    async def _price(self, m: dict, bypass: bool = False) -> float:
        t = await self._call("fetch_ticker", m["symbol"], bypass=bypass)
        price = _f(t.get("last") or t.get("close"))
        if price <= 0:
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): no price for {m['symbol']}")
        return price

    async def _held(self, bypass: bool = False) -> dict[str, tuple[Decimal, Decimal]]:
        """{coin: (total, free)} of every coin held, the quote coin left out."""
        b = await self._call("fetch_balance", bypass=bypass)
        out = {}
        for coin, total in (b.get("total") or {}).items():
            if coin == self.profile.spot_quote or not total:
                continue
            free = (b.get(coin) or {}).get("free")
            out[coin] = (Decimal(str(total)), Decimal(str(free or 0)))
        return out

    async def _avg_cost(self, m: dict, size: Decimal, bypass: bool) -> tuple[float, int] | None:
        """(average price, time of the earliest of them) of the most recent buys that make up
        this holding; None when the fills cannot be read or do not cover it."""
        try:
            trades = await self._call("fetch_my_trades", m["symbol"], None, _TRADES_LOOKBACK, bypass=bypass)
        except Exception:
            return None
        need, cost, got, first = size, Decimal(0), Decimal(0), 0
        for t in sorted(trades or [], key=lambda t: t.get("timestamp") or 0, reverse=True):
            if t.get("side") != "buy":
                continue
            take = min(Decimal(str(t.get("amount") or 0)), need - got)
            cost += take * Decimal(str(t.get("price") or 0))
            got += take
            first = t.get("timestamp") or first
            if got >= need:
                return float(cost / got), int(first)
        return None

    async def get_positions(self, category: str = "linear", symbol: str | None = None,
                            settle_coin: str | None = None, limit: int = 200,
                            bypass_breaker: bool = False) -> list[dict[str, Any]]:
        markets = await self._load()
        held = await self._held(bypass=bypass_breaker)
        wanted = [await self._market(symbol)] if symbol else [markets[f"{c}USDT"] for c in sorted(held)
                                                               if f"{c}USDT" in markets]
        out = []
        for m in wanted:
            size = held.get(m["base"], (Decimal(0), Decimal(0)))[0]
            if size <= 0:
                continue
            price = await self._price(m, bypass=bypass_breaker)
            if self._dust(m, size, price):
                continue
            triggers = await self._resting_triggers(m["symbol"], bypass=bypass_breaker)
            stop = self._stop_of(triggers, m["symbol"], True, price)
            if stop is not None and self._aged(stop):
                stop = await self._renew(m, stop, True, float(self._floor(m, size)))
            covered = stop is not None and Decimal(str(stop.get("amount") or 0)) >= self._floor(m, size)
            stop_px = _f(stop.get("stopLossPrice") or stop.get("triggerPrice") or stop.get("stopPrice")) if stop else 0
            cost = await self._avg_cost(m, size, bypass_breaker)
            out.append({
                "symbol": self._rel(m["symbol"]), "side": "Buy", "size": _num(size),
                "avgPrice": _num(cost[0]) if cost else "", "markPrice": _num(price),
                "unrealisedPnl": _num((price - cost[0]) * float(size)) if cost else "", "liqPrice": "",
                "leverage": "1", "positionIdx": 0, "stopLoss": _num(stop_px) if covered else "",
                "trailingStop": "", "createdTime": str(cost[1]) if cost else "",
            })
        return out

    async def _equity(self, bypass: bool = False) -> float:
        b = await self._call("fetch_balance", bypass=bypass)
        cash = _f((b.get(self.profile.spot_quote) or {}).get("total"))
        coins = 0.0
        for p in await self.get_positions(bypass_breaker=bypass):
            coins += _f(p["size"]) * _f(p["markPrice"])
        return cash + coins

    async def _holding(self, m: dict) -> tuple[bool, float, float] | None:
        total = (await self._held(bypass=True)).get(m["base"], (Decimal(0), Decimal(0)))[0]
        price = await self._price(m, bypass=True)
        size = self._floor(m, total)
        if self._dust(m, size, price):
            return None
        return True, float(size), price

    # ----------------------------------------------------------------- orders
    def _buy_needs_price(self) -> bool:
        """Whether CCXT turns this venue's market buy into "spend amount x price" (and needs the
        price). Elsewhere the price is NOT sent: Binance and Bybit read a price on a market order
        as an order for that much USDT, not that many coins."""
        opts = getattr(self.x, "options", None) or {}
        create = opts.get("createOrder") if isinstance(opts.get("createOrder"), dict) else {}
        return bool(opts.get("createMarketBuyOrderRequiresPrice") or create.get("createMarketBuyOrderRequiresPrice"))

    async def place_market_order(self, symbol: str, side: str, qty: str, category: str = "linear",
                                 reduce_only: bool = False, close_on_trigger: bool = False,
                                 stop_loss: str | None = None, sl_trigger_by: str = "MarkPrice",
                                 order_link_id: str | None = None) -> dict[str, Any]:
        m = await self._market(symbol)
        side = side.lower()
        if side == "sell" and not reduce_only:
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): not supported: no short selling on spot")
        params: dict[str, Any] = {}
        if order_link_id:
            params["clientOrderId"] = self._enc(order_link_id)
        if side == "buy":
            price = await self._price(m) if self._buy_needs_price() else None
            o = await self._call("create_order", m["symbol"], "market", "buy", float(qty), price, params)
            return {"orderId": str(o.get("id") or ""), "orderLinkId": order_link_id or ""}
        return await self._sell(m, Decimal(str(qty)), params, order_link_id)

    async def _sell(self, m: dict, qty: Decimal, params: dict, link: str | None) -> dict[str, Any]:
        """A closing sell (of coins, never of an amount of USDT): free the coins the relay's stop
        holds, sell, and put the stop back on what is left (or straight back if the sell is
        refused). Putting the stop back never turns a sale that went through into an error."""
        price = await self._price(m, bypass=True)
        triggers = await self._resting_triggers(m["symbol"], bypass=True)
        own = [o for o in triggers if _tag_kind(self._dec(o.get("clientOrderId"))) in (_STOP, _TAKE_PROFIT, _TRAILING)]
        stop = self._stop_of(own, m["symbol"], True, price)
        stop_px = _f(stop.get("stopLossPrice") or stop.get("triggerPrice") or stop.get("stopPrice")) if stop else 0
        await self._cancel_all(m, own)
        failure: Exception | None = None
        o: dict = {}
        try:
            free = (await self._held(bypass=True)).get(m["base"], (Decimal(0), Decimal(0)))[1]
            amount = self._floor(m, min(qty, free))
            if amount <= 0:
                raise VenueRejected(f"{REJECTED} ({self.venue_id}): insufficient balance: no {m['base']} free to sell")
            o = await self._call("create_order", m["symbol"], "market", "sell", float(amount), None, params,
                                 bypass=True)
        except Exception as e:
            failure = e
        if stop_px:
            try:
                await self._restore_stop(m, stop_px)
            except Exception as e:
                logger.error(f"[{self.venue_id}] {m['symbol']}: the stop at {stop_px} could not be put back after a "
                             f"sell ({e}); the stop audit retries")
        if failure is not None:
            raise failure
        return {"orderId": str(o.get("id") or ""), "orderLinkId": link or ""}

    async def _restore_stop(self, m: dict, stop_px: float) -> None:
        held = await self._holding(m)
        if held is None:
            return                                           # all sold: nothing left to protect
        triggers = await self._resting_triggers(m["symbol"], bypass=True)     # the audit may have been first
        r = await self._replace(m, _STOP, "sell", held[1], stop_px, triggers, self._price_param(_STOP, True, stop_px))
        if r["retCode"] not in (0, 34040):
            logger.error(f"[{self.venue_id}] {m['symbol']}: the stop at {stop_px} could not be put back after a "
                         f"sell ({r['retMsg']}); the stop audit retries")

    async def place_limit_order(self, symbol: str, side: str, qty: str, price: str, category: str = "linear",
                                post_only: bool = True, reduce_only: bool = False, order_link_id: str | None = None,
                                stop_loss: str | None = None, sl_trigger_by: str = "MarkPrice") -> dict[str, Any]:
        if reduce_only or side.lower() == "sell":
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): not supported: a resting sell beside the stop on spot")
        return await super().place_limit_order(symbol, side, qty, price, post_only=post_only,
                                                order_link_id=order_link_id)

    async def _replace(self, m: dict, kind: str, closing: str, amount: float, price, triggers: list[dict],
                       price_param: dict) -> dict[str, Any]:
        """The stop holds the coins: cancel the old one first, then place the new one, and put the
        old one back if the new one is refused."""
        old = self._own(triggers, m, kind)
        if float(price) == 0:
            return await self._cancel_all(m, old)
        if len(old) == 1:
            px = _f(old[0].get("stopLossPrice")) or _f(old[0].get("triggerPrice"))
            if (abs(px - float(price)) <= 1e-12 and _f(old[0].get("amount")) >= amount - 1e-12
                    and not self._aged(old[0])):
                return {"retCode": 34040, "retMsg": "not modified"}
        await self._cancel_all(m, old)
        tag = _tag(kind)
        params = {**price_param, **self._closing, **self._trigger_extra, "clientOrderId": self._enc(tag)}
        try:
            placed = await self._call("create_order", m["symbol"], "market", closing, amount, None, params,
                                      bypass=True)
        except (VenueRejected, ConnectionError) as e:
            if isinstance(e, ConnectionError) and await self._rests(m, tag):
                return {"retCode": 0, "retMsg": "OK"}       # the answer was lost, the order was not
            for o in old:
                back = _f(o.get("stopLossPrice")) or _f(o.get("triggerPrice"))
                try:
                    await self._call("create_order", m["symbol"], "market", closing, amount, None,
                                     {**self._price_param(kind, True, back), **self._closing,
                                      **self._trigger_extra, "clientOrderId": self._enc(_tag(kind))},
                                     bypass=True)
                except Exception as e2:
                    logger.error(f"[{self.venue_id}] {m['symbol']}: the old stop at {back} could not be put back "
                                 f"either ({e2}); the stop audit retries")
            return {"retCode": -1, "retMsg": str(e)}
        refused = await self._check_tag_echo(m, placed, tag)
        return refused or {"retCode": 0, "retMsg": "OK"}

    async def set_trading_stop(self, symbol: str, stop_loss: float | None = None, take_profit: float | None = None,
                               *args, trailing_stop: float | None = None, **kwargs) -> dict[str, Any]:
        if take_profit:
            return {"retCode": -1, "retMsg": "not supported on spot: a take-profit order beside the stop"}
        if trailing_stop:
            return {"retCode": -1, "retMsg": "not supported on spot: a trailing stop"}
        return await super().set_trading_stop(symbol, stop_loss=stop_loss, take_profit=take_profit,
                                              trailing_stop=trailing_stop)

    async def get_order(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None,
                        category: str = "linear") -> dict[str, Any] | None:
        """As on perpetuals, but a buy's filled quantity is what the account received: a fee taken
        in the coin itself (Binance without BNB, OKX, Bybit) is not held, so it is not recorded."""
        row = await super().get_order(symbol, order_id=order_id, order_link_id=order_link_id)
        if row is None or row["side"] != "Buy" or not _f(row["cumExecQty"]):
            return row
        m = await self._market(symbol)
        fee = await self._coin_fees(m, row["orderId"], row.get("createdTime"))
        if fee:
            row = {**row, "cumExecQty": _num(Decimal(row["cumExecQty"]) - fee)}
        return row

    async def _coin_fees(self, m: dict, order_id: str, created: str | None) -> Decimal:
        """The fees of this order's fills paid in the coin bought (0 when they cannot be read:
        the position sync then follows the holding)."""
        since = int(created) - 1000 if created else None
        try:
            trades = await self._call("fetch_my_trades", m["symbol"], since, _TRADES_LOOKBACK)
        except Exception as e:
            logger.warning(f"[{self.venue_id}] {m['symbol']}: fills unreadable, a fee in {m['base']} is not "
                           f"subtracted ({e})")
            return Decimal(0)
        total = Decimal(0)
        for t in trades or []:
            if str(t.get("order")) != str(order_id):
                continue
            for fee in t.get("fees") or ([t["fee"]] if t.get("fee") else []):
                if (fee or {}).get("currency") == m["base"] and fee.get("cost"):
                    total += Decimal(str(fee["cost"]))
        return total

    async def get_closed_pnl(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []               # spot fills carry no realised PnL: closes the exchange makes stay unpriced

    async def cancel_all_orders(self, category: str = "linear", symbol: str | None = None,
                                settle_coin: str | None = None, base_coin: str | None = None) -> dict[str, Any]:
        """Every open order of the relay's markets, the stops included: the symbol's, or those of
        the coins held (spot has no positions to ask for)."""
        markets = await self._load()
        if symbol:
            chosen = [await self._market(symbol)]
        else:
            held = await self._held(bypass=True)
            wanted = {f"{c}USDT" for c in held}
            try:                                          # resting buys on coins not held, too
                resting = await self._call("fetch_open_orders", None, bypass=True)
                wanted |= {self._rel(o.get("symbol")) for o in resting or [] if self._rel(o.get("symbol"))}
            except Exception as e:
                logger.warning(f"[{self.venue_id}] open orders of all markets unreadable ({e}): cancelling those "
                               f"of the coins held")
            chosen = [markets[s] for s in sorted(wanted) if s in markets]
        for m in chosen:
            plain = await self._call("fetch_open_orders", m["symbol"], bypass=True)
            for o in [*(plain or []), *await self._resting_triggers(m["symbol"], bypass=True)]:
                try:
                    await self.cancel_order(self._rel(m["symbol"]), order_id=str(o["id"]))
                except BenignOrderStateError:
                    pass
                except VenueRejected as e:
                    logger.warning(f"[{self.venue_id}] {m['symbol']}: order {o.get('id')} not cancelled ({e})")
        return {"retCode": 0, "result": {"symbols": [self._rel(m["symbol"]) for m in chosen]}}

    # ----------------------------------------------------------------- account
    async def set_leverage(self, symbol: str, leverage: float, category: str = "linear") -> dict[str, Any]:
        if float(leverage) == 1:
            return {"retCode": 110043, "retMsg": "spot: no leverage (1x)"}
        return {"retCode": -1, "retMsg": "not supported on spot: leverage"}

    async def switch_isolated_margin(self, symbol: str, leverage: float, isolated: bool = True,
                                     category: str = "linear") -> dict[str, Any]:
        return {"retCode": -1, "retMsg": "not supported on spot: a margin mode"}

    async def get_transaction_log(self, category: str = "linear", start_time: int | None = None,
                                  end_time: int | None = None, limit: int = 50, cursor: str | None = None,
                                  **kwargs) -> list[dict[str, Any]]:
        return []                                            # spot pays no funding

    async def prepare(self) -> list[str]:
        """Load the markets; say which coins the account already holds (each is taken for a
        position). No position mode to set on spot."""
        await self._load()
        held = await self.get_positions()
        if not held:
            return []
        coins = ", ".join(f"{p['size']} {p['symbol'][:-4]}" for p in held)
        return [(f"{self.venue_id}: the account holds {coins}; each is taken for one of the relay's positions "
                 f"(venue.dedicated_account)")]
