"""Template: a venue plugin (docs/plugins/venues.md).

A venue plugin connects the relay to an exchange CCXT does not reach (or reaches badly). It
answers the calls the relay makes on a venue, in the shape docs/venues.md lists ("The calls"):
the relay's engine reads exactly those fields, so a plugin that answers them can run it unchanged.

This template is a complete venue that keeps everything in memory: orders fill at once at the
price you set, and the stop sits on the position (as on Bybit). It is the reference for the
shapes, and the contract checks run against it. To connect a real exchange, keep the method
signatures and the answers' shapes, and replace each body with calls to the exchange's API.

Registering it, in your package's pyproject.toml:

    [project.entry-points."quanti_relay.venues"]
    myexchange = "your_package.module:build"       # and venue_id = "myexchange"

and in the deployment file: `venue: {exchange: myexchange, environment: testnet}`. The relay
calls `build(environment, paper=..., environ=...)` once at start. Then run
`relay verify-venue` on the exchange's test account before trusting it with anything.

Rules for a venue:
- Quantities are in coins, prices in the quote currency, as plain decimal strings.
- Keys only from `environ`, named after your exchange (RELAY_MYEXCHANGE_API_KEY, ...). On paper
  (dry-run) the relay only reads prices and market rules: do not demand keys then.
- A refusal by the exchange raises an error whose text starts with "Venue rejected"; an order
  that is already gone on cancel raises relay.util.circuit_breaker.BenignOrderStateError; a lost
  answer (network) raises anything else, and the relay then looks the order up by its tag
  (order_link_id) before it sends again. Never let a lost answer pass as a refusal.
- A stop must rest on the exchange. If it cannot, set can_stop = False: the relay then starts
  only with require_stop: false and refuses every signal that asks for a stop.
"""
from __future__ import annotations

import itertools
import time
from decimal import Decimal
from typing import Any

from relay.util.circuit_breaker import BenignOrderStateError

REJECTED = "Venue rejected"


def build(environment: str, *, paper: bool, environ) -> TemplateVenue:
    """The entry point: one client for the deployment's environment (demo, testnet or live)."""
    if environment not in ("demo", "testnet"):
        raise ValueError("the template venue is a test venue: use environment demo or testnet")
    return TemplateVenue()


def _num(value) -> str:
    return format(Decimal(str(value)).normalize(), "f") if value not in (None, "") else ""


class TemplateVenue:
    # The name the plugin is registered under (lowercase letters and digits): the relay refuses
    # a client whose venue_id is another name, because positions are recorded under it.
    venue_id = "templatevenue"
    # What it can do. Leaving one out means no: the relay then refuses the settings that need it.
    can_stop = True                # a stop rests on the venue (required: True or False)
    can_short = True               # (required: True or False)
    can_ladder = True              # reduce-only limits rest (take-profit ladders)
    can_trail = False              # a trailing stop
    can_set_leverage = True
    can_set_margin_mode = True
    gives_pnl = True               # get_closed_pnl carries the realised PnL of a close
    spot = False

    def __init__(self, prices: dict[str, float] | None = None, balance: float = 10_000.0):
        self.prices = dict(prices or {"BTCUSDT": 100.0})
        self.balance = balance
        self.positions: dict[str, dict] = {}       # symbol -> {"side", "size", "avg", "stop", "created"}
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ start
    async def prepare(self) -> list[str]:
        """Called once at start: set what the relay needs (one-way mode, ...), and return
        warnings for the log."""
        return []

    async def close(self) -> None:
        """Release connections when the relay stops."""

    # ------------------------------------------------------------------ reads
    async def list_instruments(self, category: str = "linear", symbol: str | None = None,
                               status: str | None = None) -> list[dict[str, Any]]:
        symbols = [symbol.upper()] if symbol else list(self.prices)
        return [{"symbol": s, "status": "Trading",
                 "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "1000",
                                   "minNotionalValue": "5"},
                 "priceFilter": {"tickSize": "0.1"}} for s in symbols if s in self.prices]

    async def get_ticker(self, symbol: str, category: str = "linear") -> dict[str, Any]:
        px = self._price(symbol)
        return {"symbol": symbol.upper(), "lastPrice": _num(px), "markPrice": _num(px)}

    async def get_balance_safe(self, coin: str = "USDT") -> tuple[float, str]:
        """(equity, 'wallet'), or (0.0, 'unavailable'): never raises."""
        upl = sum((self._price(s) - p["avg"]) * p["size"] * (1 if p["side"] == "Buy" else -1)
                  for s, p in self.positions.items())
        return self.balance + upl, "wallet"

    async def get_usdt_trading_balance(self) -> tuple[float, str]:
        return await self.get_balance_safe()

    async def get_positions(self, category: str = "linear", symbol: str | None = None,
                            settle_coin: str | None = None, limit: int = 200,
                            bypass_breaker: bool = False) -> list[dict[str, Any]]:
        out = []
        for s, p in self.positions.items():
            if symbol and s != symbol.upper():
                continue
            out.append({"symbol": s, "side": p["side"], "size": _num(p["size"]), "avgPrice": _num(p["avg"]),
                        "markPrice": _num(self._price(s)), "stopLoss": _num(p["stop"]) if p["stop"] else "",
                        "trailingStop": "", "unrealisedPnl": "", "createdTime": str(p["created"]),
                        "positionIdx": 0})
        return out

    async def get_open_orders(self, symbol: str | None = None, category: str = "linear",
                              settle_coin: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [self._row(o) for o in self.orders.values() if o["status"] == "New"
                and (symbol is None or o["symbol"] == symbol.upper())]

    async def get_order(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None,
                        category: str = "linear") -> dict[str, Any] | None:
        for o in self.orders.values():
            if (order_id and o["id"] == order_id) or (order_link_id and o["link"] == order_link_id):
                return self._row(o)
        return None

    async def get_order_history(self, category: str = "linear", symbol: str | None = None,
                                order_id: str | None = None, limit: int = 20,
                                order_link_id: str | None = None) -> list[dict[str, Any]]:
        if symbol and (order_id or order_link_id):
            one = await self.get_order(symbol, order_id=order_id, order_link_id=order_link_id)
            return [one] if one else []
        rows = [self._row(o) for o in self.orders.values() if symbol is None or o["symbol"] == symbol.upper()]
        return sorted(rows, key=lambda r: int(r["createdTime"]), reverse=True)[:limit]

    async def get_closed_pnl(self, category: str = "linear", symbol: str | None = None, limit: int = 50,
                             start_time: int | None = None, end_time: int | None = None,
                             max_pages: int = 1) -> list[dict[str, Any]]:
        """One record per closing order. closedPnl '' when the venue does not say (the relay then
        leaves a close the venue made unpriced, never 0)."""
        rows = [f for f in self.fills if f["closing"] and (symbol is None or f["symbol"] == symbol.upper())
                and (start_time is None or f["time"] >= start_time)]
        return [{"orderId": f["order"], "symbol": f["symbol"], "side": f["side"], "closedSize": _num(f["qty"]),
                 "avgExitPrice": _num(f["price"]), "closedPnl": _num(f["pnl"]), "createdTime": str(f["time"]),
                 "updatedTime": str(f["time"])} for f in reversed(rows)][:limit]

    async def get_transaction_log(self, category: str = "linear", start_time: int | None = None,
                                  end_time: int | None = None, max_pages: int = 200) -> list[dict[str, Any]]:
        """Funding as SETTLEMENT rows. Raise if the venue cannot say (funding stays unknown, never 0)."""
        return []

    # ------------------------------------------------------------------ orders
    async def place_market_order(self, symbol: str, side: str, qty: str, category: str = "linear",
                                 reduce_only: bool = False, close_on_trigger: bool = False,
                                 stop_loss: str | None = None, sl_trigger_by: str = "MarkPrice",
                                 order_link_id: str | None = None) -> dict[str, Any]:
        o = self._new(symbol, side, qty, "Market", None, reduce_only, order_link_id)
        self._fill(o, self._price(symbol))
        return {"orderId": o["id"], "orderLinkId": order_link_id or ""}

    async def place_limit_order(self, symbol: str, side: str, qty: str, price: str, category: str = "linear",
                                post_only: bool = True, reduce_only: bool = False, order_link_id: str | None = None,
                                stop_loss: str | None = None, sl_trigger_by: str = "MarkPrice") -> dict[str, Any]:
        o = self._new(symbol, side, qty, "Limit", float(price), reduce_only, order_link_id)
        return {"orderId": o["id"], "orderLinkId": order_link_id or ""}

    async def amend_order(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None,
                          price: str | None = None, qty: str | None = None, category: str = "linear") -> dict:
        o = self._open(order_id, order_link_id)
        if price is not None:
            o["price"] = float(price)
        if qty is not None:
            o["qty"] = Decimal(qty)
        return {"orderId": o["id"]}

    async def cancel_order(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None,
                           category: str = "linear") -> dict[str, Any]:
        o = self._open(order_id, order_link_id)
        o["status"] = "Cancelled"
        return {"orderId": o["id"]}

    async def cancel_all_orders(self, category: str = "linear", symbol: str | None = None,
                                settle_coin: str | None = None, base_coin: str | None = None) -> dict[str, Any]:
        for o in self.orders.values():
            if o["status"] == "New" and (symbol is None or o["symbol"] == symbol.upper()):
                o["status"] = "Cancelled"
        return {"retCode": 0}

    # ------------------------------------------------------------------ the stop, leverage, margin
    async def set_trading_stop(self, symbol: str, stop_loss: float | None = None, take_profit: float | None = None,
                               trailing_stop: float | None = None, **kwargs) -> dict[str, Any]:
        """{retCode: 0} set, 34040 unchanged, 10001 no position, anything else refused. 0 removes."""
        p = self.positions.get(symbol.upper())
        if p is None:
            return {"retCode": 10001, "retMsg": "can not set tp/sl/ts for zero position"}
        if take_profit or trailing_stop:
            return {"retCode": -1, "retMsg": "the template venue holds a stop only"}
        if stop_loss is None:
            return {"retCode": 0, "retMsg": "OK"}
        new = float(stop_loss) or None
        if new == p["stop"]:
            return {"retCode": 34040, "retMsg": "not modified"}
        long = p["side"] == "Buy"
        if new is not None and (new >= self._price(symbol) if long else new <= self._price(symbol)):
            return {"retCode": -1, "retMsg": "the stop is on the wrong side of the price"}
        p["stop"] = new
        return {"retCode": 0, "retMsg": "OK"}

    async def set_leverage(self, symbol: str, leverage: float, category: str = "linear") -> dict[str, Any]:
        return {"retCode": 0, "retMsg": "OK"}         # 110043 = already at that leverage

    async def switch_isolated_margin(self, symbol: str, leverage: float, isolated: bool = True,
                                     category: str = "linear") -> dict[str, Any]:
        return {"retCode": 0, "retMsg": "OK"}         # 110026 = already in that mode

    def get_circuit_breaker_status(self) -> dict[str, Any]:
        return {"is_open": False}

    # ------------------------------------------------------------------ the in-memory exchange
    def _price(self, symbol: str) -> float:
        if symbol.upper() not in self.prices:
            raise ValueError(f"{REJECTED} (templatevenue): invalid symbol: {symbol}")
        return self.prices[symbol.upper()]

    def _new(self, symbol, side, qty, kind, price, reduce_only, link) -> dict:
        self._price(symbol)
        o = {"id": f"t{next(self._ids)}", "link": link or "", "symbol": symbol.upper(), "side": side,
             "qty": Decimal(str(qty)), "type": kind, "price": price, "reduce": bool(reduce_only), "status": "New",
             "filled": Decimal(0), "avg": None, "created": int(time.time() * 1000)}
        self.orders[o["id"]] = o
        return o

    def _open(self, order_id, link) -> dict:
        for o in self.orders.values():
            if o["status"] == "New" and ((order_id and o["id"] == order_id) or (link and o["link"] == link)):
                return o
        raise BenignOrderStateError("order not exists or too late")

    def _fill(self, o: dict, px: float) -> None:
        p = self.positions.get(o["symbol"])
        qty = o["qty"]
        closing = p is not None and p["side"] != o["side"]
        if o["reduce"] and not closing:
            o["status"] = "Cancelled"
            return
        pnl = 0.0
        if closing:
            qty = min(qty, p["size"])
            pnl = (px - p["avg"]) * float(qty) * (1 if p["side"] == "Buy" else -1)
            self.balance += pnl
            p["size"] -= qty
            if p["size"] == 0:
                del self.positions[o["symbol"]]
        elif p is None:
            self.positions[o["symbol"]] = {"side": o["side"], "size": qty, "avg": px, "stop": None,
                                           "created": int(time.time() * 1000)}
        else:
            total = p["size"] + qty
            p["avg"] = (p["avg"] * float(p["size"]) + px * float(qty)) / float(total)
            p["size"] = total
        o.update(status="Filled", filled=qty, avg=px)
        self.fills.append({"order": o["id"], "symbol": o["symbol"], "side": o["side"], "qty": qty, "price": px,
                           "pnl": pnl, "closing": closing, "time": int(time.time() * 1000)})

    def _row(self, o: dict) -> dict[str, Any]:
        return {"orderId": o["id"], "orderLinkId": o["link"], "symbol": o["symbol"], "side": o["side"],
                "orderType": o["type"], "orderStatus": o["status"], "qty": _num(o["qty"]),
                "cumExecQty": _num(o["filled"]), "avgPrice": _num(o["avg"]) if o["avg"] else "",
                "price": _num(o["price"]) if o["price"] else "", "triggerPrice": "", "reduceOnly": o["reduce"],
                "createdTime": str(o["created"]), "updatedTime": str(o["created"])}
