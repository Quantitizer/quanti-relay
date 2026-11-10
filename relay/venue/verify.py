"""`relay verify-venue`: run a connector through what the relay does on an exchange, on that
exchange's demo or test account, and say step by step what worked.

It is the evidence behind a "verified" connector (docs/venues.md). It trades one small position
on one symbol, sized by the owner (no size is preset), and always cleans up after itself: the
position is closed and the relay's resting orders removed, whatever failed on the way. It never
runs on a live account, and it will not touch a symbol that already holds a position (that one is
the owner's, not the check's).

The steps (perpetuals):
  1. read      the market's rules, its price and the account balance
  2. open      a market buy, found again by the relay's tag, and the position it made
  3. stop      a stop on the exchange, shown on the position
  4. move      the stop moved: no second stop rests, and the position shows the new price
  5. same      the same stop again is "not modified", nothing new is placed
  6. ladder    a reduce-only limit rests for half the position, is found and cancelled
               (only where the exchange rests reduce-only limits)
  7. restart   a fresh connector finds the position and its stop again
  8. close     a reduce-only market sell closes it; the stop is removed; nothing of the relay's rests
  9. records   the exchange's records of the close are read (PnL per fill where it has them)
 10. funding   the funding history is read (or said to be unavailable)

On spot (venue.market: spot) step 6 is a partial sell instead: the stop must come back on what is
left. Each step is only as good as the one before it: after a failure the rest is skipped and the
cleanup runs.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any


@dataclass
class Step:
    name: str
    ok: bool | None            # None: skipped
    detail: str


class _Failed(Exception):
    pass


def _check(condition: bool, detail: str) -> None:
    if not condition:
        raise _Failed(detail)


def _floor(qty: Decimal, step: Decimal | None) -> Decimal:
    return (qty / step).to_integral_value(ROUND_DOWN) * step if step else qty


def _plain(d: Decimal) -> str:
    return format(d.normalize(), "f")


async def _own_stops(venue, symbol: str) -> list[dict]:
    return [o for o in await venue.get_open_orders(symbol=symbol) if str(o.get("orderLinkId")).startswith("rl-sl-")]


async def _position(venue, symbol: str) -> dict | None:
    rows = [p for p in await venue.get_positions(symbol=symbol) if float(p.get("size") or 0) > 0]
    return rows[0] if rows else None


async def verify_venue(venue, symbol: str, notional: float, stop_pct: float,
                       rebuild: Callable[[], Awaitable[Any]], *, can_ladder: bool = True) -> list[Step]:
    """Run the steps on this (test-account) venue. `rebuild` makes a fresh connector for the
    restart step. Returns every step, passed, failed or skipped."""
    spot = bool(getattr(venue, "spot", False))
    can_stop = bool(getattr(venue, "can_stop", True))
    steps: list[Step] = []
    state: dict[str, Any] = {}
    names = ["read", "open", "stop", "move", "same", "partial sell" if spot else "ladder", "restart", "close",
             "records", "funding"]

    async def read():
        (rules,) = await venue.list_instruments(symbol=symbol) or [None]
        _check(rules is not None, f"no market for {symbol}")
        price = float((await venue.get_ticker(symbol))["lastPrice"])
        equity, source = await venue.get_balance_safe()
        _check(source != "unavailable", "the balance cannot be read (keys, permissions?)")
        _check(await _position(venue, symbol) is None,
               f"{symbol} already holds a position on this account: the check will not touch it")
        lot = rules["lotSizeFilter"]
        step = Decimal(lot["qtyStep"]) if lot.get("qtyStep") else None
        qty = _floor(Decimal(str(notional)) / Decimal(str(price)), step)
        lowest = Decimal(lot["minOrderQty"]) if lot.get("minOrderQty") else Decimal(0)
        _check(qty > 0 and qty >= lowest, f"{notional} USDT buys {qty}, below the minimum order of {lowest}: "
                                          f"raise --notional")
        if lot.get("minNotionalValue"):
            _check(float(qty) * price >= float(lot["minNotionalValue"]),
                   f"{notional} USDT is below the minimum order value {lot['minNotionalValue']}")
        state.update(price=price, qty=qty, step=step, tick=rules["priceFilter"].get("tickSize"))
        return f"price {price}, balance {equity:.2f}, order {_plain(qty)} {symbol}"

    def _px(value: float) -> float:
        tick = state.get("tick")
        if not tick:
            return value
        return float(_floor(Decimal(str(value)), Decimal(tick)))

    async def open_():
        link = _link()
        state["opened"] = True                        # before the call: a lost answer may still have filled
        r = await venue.place_market_order(symbol, "Buy", _plain(state["qty"]), order_link_id=link)
        _check(bool(r.get("orderId")), f"no order id in the answer: {r}")
        found = await venue.get_order(symbol, order_link_id=link)
        _check(found is not None, "the order cannot be found again by the relay's tag")
        _check(found["orderStatus"] == "Filled", f"the order reads {found['orderStatus']}, not Filled")
        p = await _position(venue, symbol)
        _check(p is not None and p["side"] == "Buy", "no long position after the buy")
        state["size"] = Decimal(p["size"])
        return f"bought {p['size']} at {p.get('avgPrice') or found.get('avgPrice')}"

    async def stop():
        if not can_stop:
            raise _Skip("the exchange cannot hold a stop order (require_stop: false only)")
        px = _px(state["price"] * (1 - stop_pct / 100))
        r = await venue.set_trading_stop(symbol, stop_loss=px)
        _check(r["retCode"] == 0, f"refused: {r['retMsg']}")
        p = await _position(venue, symbol)
        _check(p is not None and p["stopLoss"] and abs(float(p["stopLoss"]) - px) < 1e-9,
               f"the position shows stop {p and p['stopLoss']!r}, not {px}")
        state["stop"] = px
        return f"stop at {px}"

    async def move():
        if "stop" not in state:
            raise _Skip("no stop")
        px = _px(state["price"] * (1 - stop_pct / 200))
        r = await venue.set_trading_stop(symbol, stop_loss=px)
        _check(r["retCode"] == 0, f"refused: {r['retMsg']}")
        stops = await _own_stops(venue, symbol)       # none where the stop sits on the position (Bybit)
        _check(len(stops) <= 1, f"{len(stops)} stops of the relay rest, not one")
        p = await _position(venue, symbol)
        _check(p is not None and p["stopLoss"] and abs(float(p["stopLoss"]) - px) < 1e-9,
               f"the position shows stop {p and p['stopLoss']!r}, not {px}")
        state["stop"] = px
        return f"moved to {px}, no second stop resting"

    async def same():
        if "stop" not in state:
            raise _Skip("no stop")
        r = await venue.set_trading_stop(symbol, stop_loss=state["stop"])
        _check(r["retCode"] == 34040, f"answered {r['retCode']} {r['retMsg']}, not 'not modified'")
        _check(len(await _own_stops(venue, symbol)) <= 1, "a second stop was placed")
        return "not modified"

    async def ladder():
        if not can_ladder:
            raise _Skip("the exchange rests no reduce-only limits (no ladder there)")
        half = _floor(state["size"] / 2, state["step"])
        px = _px(state["price"] * (1 + 2 * stop_pct / 100))
        link = _link()
        r = await venue.place_limit_order(symbol, "Sell", _plain(half), str(px), post_only=False,
                                          reduce_only=True, order_link_id=link)
        found = await venue.get_order(symbol, order_link_id=link)
        _check(found is not None and found["orderStatus"] == "New",
               f"the level does not rest: {found and found['orderStatus']}")
        await venue.cancel_order(symbol, order_id=r["orderId"])
        gone = await venue.get_order(symbol, order_id=r["orderId"])
        _check(gone is None or gone["orderStatus"] in ("Cancelled", "Deactivated"),
               f"the level reads {gone and gone['orderStatus']} after the cancel")
        return f"{_plain(half)} rested at {px} and was cancelled"

    async def partial_sell():
        if "stop" not in state:
            raise _Skip("no stop")
        half = _floor(state["size"] / 2, state["step"])
        await venue.place_market_order(symbol, "Sell", _plain(half), reduce_only=True)
        p = await _position(venue, symbol)
        _check(p is not None, "nothing left after selling half")
        stops = await _own_stops(venue, symbol)
        _check(len(stops) == 1, f"{len(stops)} stops of the relay rest after the partial sell, not one")
        _check(p["stopLoss"] and abs(float(p["stopLoss"]) - state["stop"]) < 1e-9,
               f"the rest shows stop {p['stopLoss']!r}, not {state['stop']}")
        state["size"] = Decimal(p["size"])
        return f"sold {_plain(half)}, the stop is back on {p['size']}"

    async def restart():
        fresh = await rebuild()
        try:
            p = await _position(fresh, symbol)
            _check(p is not None, "a fresh connector does not find the position")
            if "stop" in state:
                _check(p["stopLoss"] and abs(float(p["stopLoss"]) - state["stop"]) < 1e-9,
                       f"a fresh connector reads stop {p['stopLoss']!r}, not {state['stop']}")
            return f"found {p['size']}" + (f" with its stop {p['stopLoss']}" if "stop" in state else "")
        finally:
            if fresh is not venue:
                await _close_client(fresh)

    async def close():
        state["close_from"] = int(time.time() * 1000) - 60_000
        await _flatten(venue, symbol, state)
        _check(await _position(venue, symbol) is None, "a position is still open after the close")
        left = [o for o in await venue.get_open_orders(symbol=symbol) if str(o.get("orderLinkId")).startswith("rl-")]
        _check(not left, f"{len(left)} orders of the relay still rest")
        state["opened"] = False
        return "closed, nothing of the relay's rests"

    async def records():
        profile = getattr(venue, "profile", None)
        unpriced = "no PnL per fill from this exchange: a close it makes (a stop that fires) stays unpriced"
        if spot or (profile is not None and not (profile.capabilities.closed_pnl and profile.realized_pnl_field)):
            raise _Skip(unpriced)
        rows = await venue.get_closed_pnl(symbol=symbol, start_time=state["close_from"])
        if profile is None and not rows:
            raise _Skip(unpriced)                     # a plugin that keeps no such records
        _check(bool(rows), "no record of the close")
        priced = [r for r in rows if r.get("closedPnl") not in ("", None)]
        return f"{len(rows)} record(s); " + ("PnL per fill given" if priced else
                                              "no PnL per fill (closes the exchange makes stay unpriced)")

    async def funding():
        try:
            rows = await venue.get_transaction_log(start_time=state["close_from"])
        except Exception as e:
            return f"unavailable ({str(e)[:80]}): funding stays unknown, never 0"
        return f"readable ({len(rows)} entries in the window)"

    actions = [read, open_, stop, move, same, partial_sell if spot else ladder, restart, close, records, funding]
    failed = False
    try:
        for name, action in zip(names, actions, strict=True):
            if failed:
                steps.append(Step(name, None, "skipped after a failure"))
                continue
            try:
                steps.append(Step(name, True, await action()))
            except _Skip as e:
                steps.append(Step(name, None, str(e)))
            except _Failed as e:
                steps.append(Step(name, False, str(e)))
                failed = True
            except Exception as e:
                steps.append(Step(name, False, f"{type(e).__name__}: {str(e)[:200]}"))
                failed = True
    finally:
        if state.get("opened"):
            try:
                await _flatten(venue, symbol, state)
                steps.append(Step("cleanup", True, "the check's position was closed"))
            except Exception as e:
                steps.append(Step("cleanup", False, f"COULD NOT CLOSE the check's position on {symbol}: {e} "
                                                    f"- close it on the exchange by hand"))
    return steps


class _Skip(Exception):
    pass


def _link() -> str:
    """A tag in the relay's own form, owned by no deployment (the check's orders are its own)."""
    return f"rl-d0-{uuid.uuid4().hex[:12]}"


async def _flatten(venue, symbol: str, state: dict) -> None:
    """Close the check's position and remove the relay's resting orders on the symbol (the open
    orders include the stop and take-profit trigger orders)."""
    p = await _position(venue, symbol)
    if p is not None:
        await venue.place_market_order(symbol, "Sell", p["size"], reduce_only=True)
    from relay.util.circuit_breaker import BenignOrderStateError
    for o in await venue.get_open_orders(symbol=symbol):
        if str(o.get("orderLinkId")).startswith("rl-"):
            try:
                await venue.cancel_order(symbol, order_id=o["orderId"])
            except BenignOrderStateError:
                pass                                  # the venue removed it with the position


async def _close_client(venue) -> None:
    from relay.venue.factory import close_client
    await close_client(venue)


def render(steps: list[Step]) -> str:
    mark = {True: "PASS", False: "FAIL", None: "skip"}
    return "\n".join(f"{mark[s.ok]:4}  {s.name:<12} {s.detail}" for s in steps)


def ladder_offered(venue) -> bool:
    """Whether the venue rests reduce-only limits (a take-profit ladder): a CCXT venue's profile says;
    a plugin only when it declares can_ladder (what a plugin does not say it can do, it cannot)."""
    profile = getattr(venue, "profile", None)
    if profile is not None:
        return bool(profile.capabilities.reduce_only_limit)
    return getattr(venue, "can_ladder", False) is True


def passed(steps: list[Step]) -> bool:
    return all(s.ok is not False for s in steps) and any(s.ok for s in steps)
