"""A CCXT exchange in memory, for testing relay.venue.ccxt_venue without a network.

It behaves like a perpetual-futures venue that counts in contracts (BTC: 0.01 BTC each, as OKX
does) with one-way positions, market orders that fill at once at the set price, and stop /
take-profit orders that rest as separate trigger orders - visible only when listed with the
trigger flag, as on OKX or Bitget.
"""
from __future__ import annotations

import time

import ccxt


def _market(symbol, base, quote, settle, contract_size, amount_step, price_step, min_amount, swap=True,
            linear=True):
    return {"symbol": symbol, "id": symbol.replace("/", "").split(":")[0], "base": base, "quote": quote,
            "settle": settle, "swap": swap, "linear": linear, "spot": not swap, "active": True,
            "contractSize": contract_size, "precision": {"amount": amount_step, "price": price_step},
            "limits": {"amount": {"min": min_amount, "max": 100_000}, "cost": {"min": None}}}


class FakeExchange:
    precisionMode = ccxt.TICK_SIZE

    def __init__(self, price=100.0, balance=10_000.0, has=None):
        self.has = {"setPositionMode": True, "setMarginMode": True, "editOrder": True,
                    "createTrailingPercentOrder": True, "createStopLossOrder": True, "fetchFundingHistory": True, "fetchOpenOrders": True,
                    "fetchClosedOrders": True, "fetchCanceledOrders": False, **(has or {})}
        self.markets = {m["symbol"]: m for m in (
            _market("BTC/USDT:USDT", "BTC", "USDT", "USDT", 0.01, 1, 0.1, 1),
            _market("ETH/USDT:USDT", "ETH", "USDT", "USDT", 1, 0.01, 0.01, 0.01),
            _market("BTC/USDT", "BTC", "USDT", None, None, 0.0001, 0.01, 0.0001, swap=False, linear=None),
            _market("BTC/USD:BTC", "BTC", "USD", "BTC", 100, 1, 0.5, 1, linear=False),
            _market("XRP/USD:XRP", "XRP", "USD", "XRP", 10, 1, 0.0001, 1, linear=False),   # inverse only
            _market("SOL/USDC:USDC", "SOL", "USDC", "USDC", 1, 0.1, 0.01, 0.1),
        )}
        self.prices = {"BTC/USDT:USDT": price, "ETH/USDT:USDT": price, "SOL/USDC:USDC": price}
        self.balance = balance
        self.upl = 0.0
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}         # symbol -> {"contracts": signed, "entryPrice"}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail: dict[str, Exception] = {}          # method -> raised once
        self.leverage: dict[str, float] = {}
        self.margin: dict[str, str] = {}
        self.hedged = True
        self.funding: list[dict] = []
        self.trades: list[dict] = []                  # fills, each with its realised PnL in info.pnl
        self.fee = 0.1
        self.closed = False
        self._n = 0

    def _now(self):
        """Milliseconds, strictly increasing (two fills never share a time)."""
        self._clock = max(getattr(self, "_clock", 0) + 1, int(time.time() * 1000))
        return self._clock

    def _view(self, o):
        """An order as CCXT hands it to the caller."""
        return dict(o)

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        if name in self.fail:
            raise self.fail.pop(name)

    def names(self):
        return [c[0] for c in self.calls]

    async def load_markets(self, reload=False, params=None):
        self._rec("load_markets")
        return self.markets

    # orders ---------------------------------------------------------------------------------
    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        self._rec("create_order", symbol, type, side, amount, price, params=params)
        self._n += 1
        trigger = params.get("stopLossPrice") or params.get("takeProfitPrice") or params.get("triggerPrice")
        o = {"id": f"x{self._n}", "clientOrderId": params.get("clientOrderId"), "symbol": symbol, "type": type,
             "side": side, "amount": amount, "price": price, "reduceOnly": bool(params.get("reduceOnly")),
             "status": "open", "filled": 0.0, "average": None, "timestamp": self._now(),
             "stopLossPrice": params.get("stopLossPrice"), "takeProfitPrice": params.get("takeProfitPrice"),
             "triggerPrice": trigger, "trigger": bool(trigger), "params": params}
        self.orders[o["id"]] = o
        if type == "market" and not trigger:
            self._fill(o)
        return self._view(o)

    async def create_trailing_percent_order(self, symbol, type, side, amount, price=None, trailingPercent=None,
                                            trailingTriggerPrice=None, params=None):
        params = dict(params or {})
        self._rec("create_trailing_percent_order", symbol, type, side, amount, trailingPercent, trailingTriggerPrice,
                  params=params)
        self._n += 1
        o = {"id": f"x{self._n}", "clientOrderId": params.get("clientOrderId"), "symbol": symbol, "type": type,
             "side": side, "amount": amount, "price": None, "reduceOnly": True, "status": "open", "filled": 0.0,
             "average": None, "timestamp": self._now(), "trailingPercent": trailingPercent,
             "triggerPrice": trailingTriggerPrice, "trigger": True}
        self.orders[o["id"]] = o
        return self._view(o)

    def _fill(self, o):
        sym, px = o["symbol"], self.prices[o["symbol"]]
        signed = o["amount"] if o["side"] == "buy" else -o["amount"]
        pos = self.positions.get(sym, {"contracts": 0.0, "entryPrice": 0.0})
        held = pos["contracts"]
        if o["reduceOnly"]:
            if held == 0 or (held > 0) == (signed > 0):
                o.update(status="canceled")
                return
            signed = max(signed, -held) if held > 0 else min(signed, -held)
        new = round(held + signed, 10)
        if held == 0 or (held > 0) == (signed > 0):
            entry = (abs(held) * pos["entryPrice"] + abs(signed) * px) / abs(new)
            pnl = 0.0
        else:
            entry = pos["entryPrice"]
            cs = self.markets[sym]["contractSize"]
            pnl = (px - entry) * abs(signed) * cs * (1 if held > 0 else -1)
        self.trades.append({"id": f"t{len(self.trades) + 1}", "order": o["id"], "symbol": sym, "side": o["side"],
                            "amount": abs(signed), "price": px, "timestamp": self._now(),
                            "fee": {"cost": self.fee, "currency": "USDT"}, "info": {"pnl": str(pnl)}})
        if new == 0:
            self.positions.pop(sym, None)
        else:
            self.positions[sym] = {"contracts": new, "entryPrice": entry}
        o.update(status="closed", filled=abs(signed), average=px)

    def trigger(self, symbol):
        """The price moved: fire the resting stop orders it crossed."""
        px = self.prices[symbol]
        for o in list(self.orders.values()):
            if o["symbol"] != symbol or o["status"] != "open" or not o.get("stopLossPrice"):
                continue
            if (o["side"] == "sell" and px <= o["stopLossPrice"]) or (o["side"] == "buy" and px >= o["stopLossPrice"]):
                self._fill(o)

    def _find(self, id=None, params=None):
        params = params or {}
        o = self.orders.get(id) if id else next(
            (x for x in self.orders.values() if params.get("clientOrderId")
             and x["clientOrderId"] == params["clientOrderId"]), None)
        if o is None or (o.get("trigger") and not params.get("trigger")):
            raise ccxt.OrderNotFound(f"fake order {id or params} does not exist")
        return o

    async def fetch_order(self, id, symbol=None, params=None):
        self._rec("fetch_order", id, symbol, params=params)
        return self._view(self._find(id, params))

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        params = params or {}
        self._rec("fetch_open_orders", symbol, params=params)
        return [self._view(o) for o in self.orders.values() if o["status"] == "open"
                and (symbol is None or o["symbol"] == symbol) and bool(o.get("trigger")) == bool(params.get("trigger"))]

    async def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        self._rec("fetch_closed_orders", symbol)
        return [self._view(o) for o in self.orders.values() if o["status"] != "open"
                and (symbol is None or o["symbol"] == symbol)]

    async def cancel_order(self, id, symbol=None, params=None):
        self._rec("cancel_order", id, symbol, params=params)
        o = self._find(id, params)
        if o["status"] != "open":
            raise ccxt.OrderNotFound(f"fake order {id} is not open")
        o["status"] = "canceled"
        return self._view(o)

    async def cancel_all_orders(self, symbol=None, params=None):
        params = params or {}
        self._rec("cancel_all_orders", symbol, params=params)
        for o in self.orders.values():
            if o["status"] == "open" and o["symbol"] == symbol and bool(o.get("trigger")) == bool(params.get("trigger")):
                o["status"] = "canceled"
        return []

    async def edit_order(self, id, symbol, type, side, amount=None, price=None, params=None):
        self._rec("edit_order", id, symbol, type, side, amount, price)
        o = self._find(id)
        if o["status"] != "open":
            raise ccxt.OrderNotFound(f"fake order {id} is not open")
        o.update(amount=amount if amount is not None else o["amount"], price=price if price is not None else o["price"])
        return self._view(o)

    # account --------------------------------------------------------------------------------
    async def fetch_positions(self, symbols=None, params=None):
        self._rec("fetch_positions", symbols)
        out = []
        for sym, p in self.positions.items():
            if symbols and sym not in symbols:
                continue
            out.append({"symbol": sym, "side": "long" if p["contracts"] > 0 else "short",
                        "contracts": abs(p["contracts"]), "entryPrice": p["entryPrice"],
                        "markPrice": self.prices[sym], "unrealizedPnl": self.upl, "leverage": 2,
                        "liquidationPrice": None, "timestamp": self._now()})
        return out

    async def fetch_ticker(self, symbol, params=None):
        self._rec("fetch_ticker", symbol)
        px = self.prices[symbol]
        return {"symbol": symbol, "last": px, "bid": px - 0.1, "ask": px + 0.1, "markPrice": px,
                "baseVolume": 1000, "quoteVolume": 100_000}

    async def fetch_balance(self, params=None):
        self._rec("fetch_balance")
        return {"USDT": {"total": self.balance, "free": self.balance}, "total": {"USDT": self.balance}}

    async def set_leverage(self, leverage, symbol=None, params=None):
        self._rec("set_leverage", leverage, symbol)
        if self.leverage.get(symbol) == leverage:
            raise ccxt.BadRequest("fake leverage not modified")
        self.leverage[symbol] = leverage

    async def set_margin_mode(self, marginMode, symbol=None, params=None):
        self._rec("set_margin_mode", marginMode, symbol, params=params)
        if self.margin.get(symbol) == marginMode:
            raise ccxt.BadRequest("fake margin mode not modified")
        self.margin[symbol] = marginMode

    async def set_position_mode(self, hedged, symbol=None, params=None):
        self._rec("set_position_mode", hedged)
        if self.hedged == hedged:
            raise ccxt.BadRequest("fake no need to change position mode")
        self.hedged = hedged

    async def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self._rec("fetch_my_trades", symbol, since, limit)
        rows = [t for t in self.trades if (symbol is None or t["symbol"] == symbol)
                and (since is None or t["timestamp"] >= since)]
        return rows[:limit] if limit else rows

    async def fetch_funding_history(self, symbol=None, since=None, limit=None, params=None):
        self._rec("fetch_funding_history", symbol, since)
        return [f for f in self.funding if since is None or f["timestamp"] >= since]

    async def close(self):
        self.closed = True


class FakeMexc(FakeExchange):
    """MEXC as CCXT 4.5.89 presents it: stops are plan orders asked for with a plain triggerPrice
    and the direction (triggerType 1: price rises to it, 2: falls to it); CCXT reads neither the
    client id (it is in info.externalOid) nor the side codes 2 and 4 (2 close short reads 'sell',
    4 close long reads '4'), and markets the API takes no orders on say apiAllowed false."""

    def __init__(self, echo=True, **kw):
        super().__init__(**kw)
        self.echo = echo                              # whether plan orders give externalOid back
        self.has.pop("createStopLossOrder")
        self.has["createTriggerOrder"] = True
        self.markets["ETH/USDT:USDT"]["info"] = {"apiAllowed": False}

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        assert not params.get("stopLossPrice") and not params.get("takeProfitPrice"), "MEXC: use triggerPrice"
        if params.get("triggerPrice"):
            assert params.get("triggerType") in (1, 2)
        o = await super().create_order(symbol, type, side, amount, price, params)
        self.orders[o["id"]]["triggerType"] = params.get("triggerType")
        return self._view(self.orders[o["id"]])

    def trigger(self, symbol):
        px = self.prices[symbol]
        for o in list(self.orders.values()):
            crossed = o.get("triggerType") and ((px >= o["triggerPrice"]) if o["triggerType"] == 1
                                                 else (px <= o["triggerPrice"]))
            if o["symbol"] == symbol and o["status"] == "open" and crossed:
                self._fill(o)

    def _view(self, o):
        code = ("2" if o["reduceOnly"] else "1") if o["side"] == "buy" else ("4" if o["reduceOnly"] else "3")
        echoed = o.get("clientOrderId") if self.echo or not o.get("trigger") else None
        return dict(o, clientOrderId=None, reduceOnly=False, side={"1": "buy", "2": "sell"}.get(code, code),
                    info={"externalOid": echoed, "side": code})

    async def cancel_all_orders(self, symbol=None, params=None):
        """MEXC picks the plan-order endpoint by a method name; a trigger flag is ignored."""
        params = params or {}
        self._rec("cancel_all_orders", symbol, params=params)
        plan = params.get("method") == "contractPrivatePostPlanorderCancelAll"
        for o in self.orders.values():
            if o["status"] == "open" and o["symbol"] == symbol and bool(o.get("trigger")) == plan:
                o["status"] = "canceled"
        return []


class FakeSpot:
    """A spot exchange in memory: coins bought with USDT, a resting stop-market sell holds the coins
    it would sell (they are not free for another order), and stop orders are listed only with the
    trigger flag (as on OKX or Bitget)."""
    precisionMode = ccxt.TICK_SIZE

    def __init__(self, price=100.0, usdt=10_000.0, features=None, coin_fee=0.0, options=None):
        self.has = {"fetchOpenOrders": True, "fetchClosedOrders": True, "fetchMyTrades": True}
        self.options = dict(options or {})
        self.coin_fee = coin_fee                      # fraction of a buy kept as the fee, in the coin
        self.drop_tags = False                        # True: stop orders come back without their tag
        self.features = {"spot": {"createOrder": {"stopLossPrice": True}}} if features is None else features
        self.markets = {m["symbol"]: m for m in (
            _market("BTC/USDT", "BTC", "USDT", None, None, 0.0001, 0.01, 0.0001, swap=False, linear=None),
            _market("ETH/USDT", "ETH", "USDT", None, None, 0.001, 0.01, 0.001, swap=False, linear=None),
            _market("BTC/USDT:USDT", "BTC", "USDT", "USDT", 0.01, 1, 0.1, 1),             # a perpetual: not spot
            _market("SOL/EUR", "SOL", "EUR", None, None, 0.01, 0.01, 0.01, swap=False, linear=None),  # not USDT
        )}
        for m in self.markets.values():
            m["limits"]["cost"]["min"] = 5.0
        self.prices = {"BTC/USDT": price, "ETH/USDT": price}
        self.total = {"USDT": usdt}
        self.locked: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail: dict[str, Exception] = {}
        self._n = 0
        self._clock = 0

    def _now(self):
        self._clock = max(self._clock + 1, int(time.time() * 1000))
        return self._clock

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        if name in self.fail:
            raise self.fail.pop(name)

    def names(self):
        return [c[0] for c in self.calls]

    def free(self, coin):
        return round(self.total.get(coin, 0.0) - self.locked.get(coin, 0.0), 12)

    async def load_markets(self, reload=False, params=None):
        self._rec("load_markets")
        return self.markets

    async def fetch_ticker(self, symbol, params=None):
        self._rec("fetch_ticker", symbol)
        px = self.prices[symbol]
        return {"symbol": symbol, "last": px, "bid": px, "ask": px}

    async def fetch_balance(self, params=None):
        self._rec("fetch_balance")
        out = {"total": dict(self.total)}
        for coin, total in self.total.items():
            out[coin] = {"total": total, "free": self.free(coin), "used": self.locked.get(coin, 0.0)}
        return out

    def _trade(self, o, amount):
        sym, px = o["symbol"], self.prices[o["symbol"]]
        base = self.markets[sym]["base"]
        fee = None
        if o["side"] == "buy":
            self.total["USDT"] -= amount * px
            fee = round(amount * self.coin_fee, 12)
            self.total[base] = round(self.total.get(base, 0.0) + amount - fee, 12)
        else:
            self.total[base] = round(self.total[base] - amount, 12)
            self.total["USDT"] += amount * px
        self.trades.append({"id": f"t{len(self.trades) + 1}", "order": o["id"], "symbol": sym, "side": o["side"],
                            "amount": amount, "price": px, "timestamp": self._now(), "info": {},
                            "fee": {"cost": fee, "currency": base} if fee else {"cost": 0.1, "currency": "USDT"}})
        o.update(status="closed", filled=amount, average=px)

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        self._rec("create_order", symbol, type, side, amount, price, params=params)
        assert "reduceOnly" not in params, "spot takes no reduceOnly"
        base = self.markets[symbol]["base"]
        if type == "market" and price is not None and not (params.get("stopLossPrice") or params.get("triggerPrice")):
            # As Binance and Bybit do: a price on a market order makes it an order for that much USDT.
            amount = round(amount * price / self.prices[symbol], 12)
        self._n += 1
        trigger = params.get("stopLossPrice") or params.get("triggerPrice")
        o = {"id": f"s{self._n}", "clientOrderId": None if self.drop_tags and trigger else params.get("clientOrderId"),
             "symbol": symbol, "type": type, "side": side, "amount": amount, "price": price, "status": "open",
             "filled": 0.0, "average": None, "timestamp": self._now(), "stopLossPrice": params.get("stopLossPrice"),
             "triggerPrice": trigger, "trigger": bool(trigger), "params": params}
        if side == "sell" and self.free(base) < amount - 1e-12:
            raise ccxt.InsufficientFunds(f"fake: {amount} {base} not free ({self.free(base)})")
        if side == "buy" and self.free("USDT") < amount * self.prices[symbol]:
            raise ccxt.InsufficientFunds("fake: not enough USDT")
        self.orders[o["id"]] = o
        if trigger:
            self.locked[base] = self.locked.get(base, 0.0) + amount
        elif type == "market":
            self._trade(o, amount)
        return dict(o)

    def trigger(self, symbol):
        px = self.prices[symbol]
        for o in list(self.orders.values()):
            if o["symbol"] == symbol and o["status"] == "open" and o.get("triggerPrice") and px <= o["triggerPrice"]:
                base = self.markets[symbol]["base"]
                self.locked[base] -= o["amount"]
                self._trade(o, o["amount"])

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        params = params or {}
        self._rec("fetch_open_orders", symbol, params=params)
        return [dict(o) for o in self.orders.values() if o["status"] == "open"
                and (symbol is None or o["symbol"] == symbol) and bool(o.get("trigger")) == bool(params.get("trigger"))]

    async def fetch_order(self, id, symbol=None, params=None):
        self._rec("fetch_order", id, symbol, params=params)
        if id not in self.orders:
            raise ccxt.OrderNotFound(f"fake order {id}")
        return dict(self.orders[id])

    async def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        self._rec("fetch_closed_orders", symbol)
        return [dict(o) for o in self.orders.values() if o["status"] != "open"]

    async def cancel_order(self, id, symbol=None, params=None):
        self._rec("cancel_order", id, symbol, params=params)
        o = self.orders.get(id)
        if o is None or o["status"] != "open":
            raise ccxt.OrderNotFound(f"fake order {id} is not open")
        o["status"] = "canceled"
        if o.get("trigger"):
            base = self.markets[o["symbol"]]["base"]
            self.locked[base] -= o["amount"]
        return dict(o)

    async def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self._rec("fetch_my_trades", symbol, since, limit)
        return [t for t in self.trades if symbol is None or t["symbol"] == symbol]

    async def close(self):
        pass


class FakeSplitLists(FakeExchange):
    """A venue that files each kind of trigger order in its own list (HTX: stop, take-profit and
    trailing apart; OKX: conditional orders apart from plain triggers). An order is listed,
    fetched and cancelled only with its kind's parameters; `also` names more parameter sets that
    reach a kind for cancel / fetch (OKX: the trigger flag reaches conditional orders)."""

    def __init__(self, kinds, also=None, plain_lists_triggers=False, cancel_all=True, cancel_with=None, **kw):
        super().__init__(**kw)
        self.kinds = kinds                            # {"sl": params, "tp": params, "ts": params}
        self.also = also or {}
        self.cancel_with = cancel_with or {}          # kind -> the only params that cancel / fetch it (Bitget)
        self.plain_lists_triggers = plain_lists_triggers
        if not cancel_all:
            self.has["cancelAllOrders"] = False

    def _kind(self, o):
        return "sl" if o.get("stopLossPrice") else "tp" if o.get("takeProfitPrice") else \
            "ts" if o.get("trailingPercent") else None

    def _reaches(self, o, params):
        kind = self._kind(o)
        if kind is None:
            return not params
        if self.plain_lists_triggers and not params:
            return True
        return params == self.kinds[kind] or params in self.also.get(kind, [])

    def _find(self, id=None, params=None):
        o = self.orders.get(id)
        kind = self._kind(o) if o else None
        if o is not None and kind in self.cancel_with:
            if (params or {}) != self.cancel_with[kind]:
                raise ccxt.OrderNotFound(f"fake order {id} not reached with {params}")
            return o
        if o is None or not self._reaches(o, params or {}):
            raise ccxt.OrderNotFound(f"fake order {id} not in the list {params}")
        return o

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        params = params or {}
        self._rec("fetch_open_orders", symbol, params=params)
        if params and params not in self.kinds.values():
            raise ccxt.BadRequest(f"fake: unknown list {params}")
        return [self._view(o) for o in self.orders.values() if o["status"] == "open"
                and (symbol is None or o["symbol"] == symbol)
                and (self._reaches(o, params) if not params else self._kind(o) and self.kinds[self._kind(o)] == params)]

    async def cancel_all_orders(self, symbol=None, params=None):
        self._rec("cancel_all_orders", symbol, params=params)
        if self.has.get("cancelAllOrders") is False:
            raise ccxt.NotSupported("fake: no cancel-all")
        for o in self.orders.values():
            if o["status"] == "open" and o["symbol"] == symbol and self._kind(o) is None and not params:
                o["status"] = "canceled"
        return []
