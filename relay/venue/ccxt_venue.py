"""Any exchange CCXT reaches, behind the same calls the relay makes to Bybit.

The execution code was built on Bybit's v5 interface and reads Bybit's response fields. Rather than
rewrite that well-tested code, this connector answers each of those calls in Bybit's shape, from
CCXT's unified interface (docs/venues.md lists the calls and the fields). An exchange's quirks live
in its profile (relay.venue.profiles), not here.

What it does for the relay:
- Symbols: the relay's BTCUSDT is the exchange's USDT-settled linear perpetual of BTC (BTC/USDT:USDT
  in CCXT; for a USDC-settled venue, BTC/USDC:USDC). Spot, inverse and dated futures are never used.
- Quantities: the relay counts in coins. Many exchanges count perpetuals in contracts (one OKX BTC
  contract is 0.01 BTC); every quantity is converted both ways with the market's contract size, in
  decimal arithmetic, so 0.03 BTC is never 2.9999 contracts.
- The stop: Bybit keeps a stop on the position itself; elsewhere it is a reduce-only stop-market
  order resting on the exchange. set_trading_stop places the new one BEFORE it cancels the old one
  (the position is never without a stop), and get_positions reports the stop from the resting
  order, only when it covers the whole position, so a position that grew is re-protected by the
  stop audit.
- Client order ids: the relay's tags (rl-d<deployment>-<hex>) are rewritten into the form the
  exchange accepts and read back, so the relay still recognises its own orders.
- Errors: a refusal by the exchange raises "Venue rejected (<exchange>): <kind>: <message>"; a
  network failure raises a plain error (the outcome is unknown and the relay looks the order up);
  an order already gone on cancel is the benign "order not exists or too late".
- Answers Bybit gives as a return code (leverage, margin mode, set_trading_stop) come back as a dict
  with retCode: 0, the "not modified" codes 110043 / 110026, or -1 with the reason.

Not offered unless the profile says the venue can: trailing stops, the venue's own realised-PnL
records (the relay then works from its fills) and the funding history (funding then stays unknown,
never 0).
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from decimal import Decimal
from typing import Any

from loguru import logger

from relay.config import settings
from relay.util.circuit_breaker import BenignOrderStateError, CircuitBreaker
from relay.venue.profiles import Profile

REJECTED = "Venue rejected"
_STOP, _TAKE_PROFIT, _TRAILING = "sl", "tp", "ts"
_LOOKUPS = (("fetch_open_orders", "fetchOpenOrders"), ("fetch_closed_orders", "fetchClosedOrders"),
            ("fetch_canceled_orders", "fetchCanceledOrders"))


class VenueRejected(Exception):
    """The exchange refused the request (as opposed to: no answer)."""


def _num(value) -> str:
    """A number as a plain decimal string ('' for none); never '1e-05'."""
    if value is None or value == "":
        return ""
    d = Decimal(str(value))
    text = format(d.normalize(), "f")
    return text if text not in ("-0",) else "0"


def _f(value) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- client order ids
_LINK = re.compile(r"^rl-d(\d+)-([0-9a-f]+)$")
_TAG = re.compile(r"^rl-(sl|tp|ts)-([0-9a-f]+)$")
_ALNUM_LINK = re.compile(r"^rld(\d+)x([0-9a-f]+)$")
_ALNUM_TAG = re.compile(r"^rl(sl|tp|ts)([0-9a-f]+)$")
_HEX_KINDS = {"d": "1", _STOP: "2", _TAKE_PROFIT: "3", _TRAILING: "4"}
_HEX_KIND_NAMES = {v: k for k, v in _HEX_KINDS.items()}


def encode_client_id(link: str | None, scheme: str) -> str | None:
    """The relay's tag in the form the exchange accepts (see Profile.client_id)."""
    if not link:
        return None
    if scheme == "plain":
        return link
    if scheme == "prefix_t":
        return f"t-{link}"
    m, t = _LINK.match(link), _TAG.match(link)
    if scheme == "alnum":
        if m:
            return f"rld{m.group(1)}x{m.group(2)}"
        if t:
            return f"rl{t.group(1)}{t.group(2)}"
        return re.sub(r"[^A-Za-z0-9]", "", link)[:32]
    if scheme == "hex128":
        if m:
            dep, rand = int(m.group(1)), m.group(2)
            return "0xa7" + _HEX_KINDS["d"] + f"{dep:08x}" + rand[:14].ljust(14, "0") + "0" * 7
        if t:
            return "0xa7" + _HEX_KINDS[t.group(1)] + "0" * 8 + t.group(2)[:14].ljust(14, "0") + "0" * 7
        return "0x" + uuid.uuid4().hex
    if scheme == "int63":
        if m:
            return f"9{_HEX_KINDS['d']}{int(m.group(1)) % 1_000_000:06d}{int(m.group(2)[:8], 16):010d}"
        if t:
            return f"9{_HEX_KINDS[t.group(1)]}000000{int(t.group(2)[:8], 16):010d}"
        return str(int(uuid.uuid4().hex[:15], 16))
    raise ValueError(f"unknown client id scheme {scheme!r}")


def decode_client_id(value: str | None, scheme: str) -> str:
    """The relay's tag back from the exchange's client order id ('' for none; a foreign id as is)."""
    if not value:
        return ""
    value = str(value)
    if scheme == "prefix_t" and value.startswith("t-"):
        return value[2:]
    if scheme == "alnum":
        m, t = _ALNUM_LINK.match(value), _ALNUM_TAG.match(value)
        if m:
            return f"rl-d{m.group(1)}-{m.group(2)}"
        if t:
            return f"rl-{t.group(1)}-{t.group(2)}"
    if scheme == "int63" and len(value) == 18 and value.isdigit() and value[0] == "9":
        kind = _HEX_KIND_NAMES.get(value[1])
        rand = f"{int(value[8:]):08x}"
        if kind == "d":
            return f"rl-d{int(value[2:8])}-{rand}"
        if kind:
            return f"rl-{kind}-{rand}"
    if scheme == "hex128" and value.lower().startswith("0xa7") and len(value) == 34:
        v = value.lower()
        kind = _HEX_KIND_NAMES.get(v[4])
        if kind == "d":
            return f"rl-d{int(v[5:13], 16)}-{v[13:27]}"
        if kind:
            return f"rl-{kind}-{v[13:25]}"
    return value


def _tag(kind: str) -> str:
    return f"rl-{kind}-{uuid.uuid4().hex[:12]}"


def _tag_kind(link: str) -> str | None:
    t = _TAG.match(link or "")
    return t.group(1) if t else None


# --------------------------------------------------------------------- the connector
class CcxtVenue:
    """A CCXT exchange (async_support instance) behind the relay's venue calls."""
    spot = False                       # the perpetuals (relay.venue.ccxt_spot is the spot market)
    can_short = True

    def __init__(self, exchange, profile: Profile):
        self.x = exchange
        self.profile = profile
        self.venue_id = profile.exchange
        self.circuit_breaker = CircuitBreaker(
            name=f"{profile.exchange}_trading",
            failure_threshold=settings.circuit_breaker_failure_threshold,
            timeout=settings.circuit_breaker_timeout,
            on_state_change=lambda old, new: (logger.warning if new == "open" else logger.info)(
                f"[{profile.exchange}] circuit breaker {old} -> {new}"),
        )
        # How resting trigger orders are listed, and what marks an order as one that only closes.
        self._trigger_params: dict = dict(profile.trigger_list_params)
        # Where the resting trigger orders are listed: one call per kind where the venue keeps
        # them apart (OKX: conditional and trailing; HTX: stop, take-profit and trailing).
        self._listings: tuple[dict, ...] = tuple(profile.trigger_listings or (self._trigger_params,))
        self._trigger_extra: dict = dict(profile.trigger_params)
        self._cancel_params: tuple[dict, ...] = tuple(profile.trigger_cancel_params or ())
        self._max_age: int | None = profile.trigger_max_age_s
        self._closing: dict = {"reduceOnly": True}
        # Whether the venue gives the relay's tag back on its trigger orders: None until the first
        # stop shows it, then True, or False (stops are then refused: the relay could not tell its
        # own stop from another and would pile them up).
        self._tags_echoed: bool | None = None
        self._markets: dict[str, dict] | None = None      # relay symbol -> CCXT market
        self._relay_symbol: dict[str, str] = {}             # CCXT symbol -> relay symbol
        self._load_lock = asyncio.Lock()

    # ----------------------------------------------------------------- plumbing
    async def _call(self, method: str, *args, bypass: bool = False, **kwargs):
        fn = getattr(self.x, method)

        async def run():
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                raise self._translate(e, method) from e
        result = await self.circuit_breaker.call_async(run, bypass_open=bypass)
        if "order" in method and (self.profile.order_info or self.profile.order_sides):
            for o in result if isinstance(result, list) else [result]:
                self._complete(o)
        return result

    @property
    def has_keys(self) -> bool:
        """Whether the exchange was given every key its profile needs (on paper without them only
        public data is read; a half-set counts as none)."""
        return all(getattr(self.x, cred, None) for cred in self.profile.credentials)

    @property
    def can_stop(self) -> bool:
        """Whether a stop order can rest on this exchange (else an entry asking for one is refused)."""
        from relay.venue.factory import stop_supported
        # Also no once the venue has shown it drops the relay's tag on stop orders: entries that
        # ask for a stop are then refused up front instead of opened and closed again.
        return stop_supported(self.profile, self.x.has) and self._tags_echoed is not False

    def _complete(self, o) -> None:
        """Fill in the order fields CCXT left unread on this venue, from the raw answer."""
        if not isinstance(o, dict):
            return
        info = o.get("info") if isinstance(o.get("info"), dict) else {}
        for unified, raw in self.profile.order_info.items():
            if not o.get(unified) and info.get(raw) not in (None, ""):
                o[unified] = str(info[raw])
        side = self.profile.order_sides.get(str(info.get("side")))
        if side:
            o["side"] = side[0]
            if side[1]:
                o["reduceOnly"] = True

    def _translate(self, e: Exception, method: str) -> Exception:
        import ccxt
        name = self.profile.exchange
        text = str(e)[:300]
        if isinstance(e, ccxt.OrderNotFound) and method in ("cancel_order", "edit_order"):
            return BenignOrderStateError(f"order not exists or too late ({name}: {text})")
        if isinstance(e, ccxt.NetworkError) and not isinstance(e, ccxt.RateLimitExceeded):
            return ConnectionError(f"{name} connection error: {text}")
        kind = ("insufficient balance" if isinstance(e, ccxt.InsufficientFunds)
                else "order not found" if isinstance(e, ccxt.OrderNotFound)
                else "invalid order" if isinstance(e, ccxt.InvalidOrder)
                else "invalid symbol" if isinstance(e, ccxt.BadSymbol)
                else "authentication failed" if isinstance(e, (ccxt.AuthenticationError, ccxt.PermissionDenied))
                else "rate limit (too many requests)" if isinstance(e, ccxt.RateLimitExceeded)
                else "not supported" if isinstance(e, ccxt.NotSupported)
                else "error")
        if isinstance(e, ccxt.BaseError):
            return VenueRejected(f"{REJECTED} ({name}): {kind}: {text}")
        return e

    async def _load(self) -> dict[str, dict]:
        if self._markets is not None:
            return self._markets
        async with self._load_lock:
            if self._markets is None:
                markets = await self._call("load_markets")
                settle = self.profile.settle
                found: dict[str, dict] = {}
                for m in (markets or {}).values():
                    if not (m.get("swap") and m.get("linear") and m.get("settle") == settle
                            and m.get("quote") == settle and m.get("active") is not False):
                        continue
                    if str((m.get("info") or {}).get("apiAllowed", "")).lower() == "false":
                        continue                           # the venue takes no API orders on it (MEXC)
                    for key in {f"{m['base']}USDT", f"{m['base']}{settle}"}:
                        if key in found and found[key]["symbol"] != m["symbol"]:
                            logger.warning(f"[{self.venue_id}] two markets for {key}: kept {found[key]['symbol']}")
                            continue
                        found[key] = m
                    self._relay_symbol[m["symbol"]] = f"{m['base']}USDT"
                self._markets = found
        return self._markets

    async def _market(self, symbol: str) -> dict:
        m = (await self._load()).get(str(symbol).upper())
        if m is None:
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): invalid symbol: no {self.profile.settle}-settled "
                                f"perpetual for {symbol}")
        return m

    def _rel(self, ccxt_symbol: str | None) -> str | None:
        return self._relay_symbol.get(ccxt_symbol or "")

    @staticmethod
    def _cs(m: dict) -> Decimal:
        return Decimal(str(m.get("contractSize") or 1))

    def _to_amount(self, m: dict, qty) -> float:
        """Coins -> the exchange's amount (contracts)."""
        return float(Decimal(str(qty)) / self._cs(m))

    def _to_coins(self, m: dict, amount) -> Decimal:
        return Decimal(str(amount or 0)) * self._cs(m)

    def _step(self, value) -> Decimal | None:
        """A precision value as a step (CCXT gives a step, or a number of decimals)."""
        if value in (None, ""):
            return None
        import ccxt
        if getattr(self.x, "precisionMode", None) == ccxt.DECIMAL_PLACES:
            return Decimal(1).scaleb(-int(value))
        return Decimal(str(value))

    def _enc(self, link: str | None) -> str | None:
        return encode_client_id(link, self.profile.client_id)

    def _dec(self, value: str | None) -> str:
        return decode_client_id(value, self.profile.client_id)

    def _order_row(self, o: dict, m: dict | None = None) -> dict[str, Any]:
        m = m or (self._markets or {}).get(self._rel(o.get("symbol")) or "", {})
        filled = _f(o.get("filled"))
        status = o.get("status")
        bybit_status = {"open": "PartiallyFilled" if filled > 0 else "New", "closed": "Filled",
                        "rejected": "Rejected"}.get(status)
        if status in ("canceled", "cancelled", "expired"):
            bybit_status = "PartiallyFilledCanceled" if filled > 0 else ("Cancelled" if status != "expired"
                                                                          else "Deactivated")
        trigger = o.get("triggerPrice") or o.get("stopPrice") or o.get("stopLossPrice") or o.get("takeProfitPrice")
        return {
            "orderId": str(o.get("id") or ""), "orderLinkId": self._dec(o.get("clientOrderId")),
            "symbol": self._rel(o.get("symbol")) or o.get("symbol"), "side": "Buy" if o.get("side") == "buy" else "Sell",
            "orderStatus": bybit_status or "New", "orderType": "Limit" if o.get("type") == "limit" else "Market",
            "qty": _num(self._to_coins(m, o.get("amount"))) if m else _num(o.get("amount")),
            "cumExecQty": _num(self._to_coins(m, filled)) if m else _num(filled),
            "avgPrice": _num(o.get("average")) if o.get("average") else "",
            "price": _num(o.get("price")) if o.get("price") else "",
            "triggerPrice": _num(trigger) if trigger else "",
            "stopLossPrice": _num(o.get("stopLossPrice")) if o.get("stopLossPrice") else "",
            "takeProfitPrice": _num(o.get("takeProfitPrice")) if o.get("takeProfitPrice") else "",
            "reduceOnly": bool(o.get("reduceOnly")),
            "createdTime": str(o.get("timestamp") or ""),
            "updatedTime": str(o.get("lastUpdateTimestamp") or o.get("timestamp") or ""),
        }

    # ----------------------------------------------------------------- setup
    async def prepare(self) -> list[str]:
        """Load the markets and put the account in one-way mode. Returns warnings (empty: all
        set). Called once when the relay starts."""
        warnings: list[str] = []
        await self._load()
        # Without keys there is no account to set (paper on prices only): nothing to warn about.
        if self.has_keys and self.profile.capabilities.one_way and self.x.has.get("setPositionMode"):
            try:
                await self._call("set_position_mode", False)
            except Exception as e:
                if not _not_modified(str(e)):
                    warnings.append(f"one-way position mode could not be set ({e}); the relay needs one "
                                    f"position per symbol - set it in the exchange's settings")
        return warnings

    async def close(self) -> None:
        await self.x.close()

    def get_circuit_breaker_status(self) -> dict:
        status = self.circuit_breaker.get_status()
        remaining = None
        if status["state"] == "open" and status.get("last_failure_time"):
            remaining = max(0, self.circuit_breaker.timeout - (time.time() - status["last_failure_time"]))
        return {"state": status["state"], "is_open": status["state"] == "open",
                "failure_count": status["failure_count"], "last_failure_time": status.get("last_failure_time"),
                "seconds_until_recovery": remaining}

    # ----------------------------------------------------------------- reads
    async def list_instruments(self, category: str = "linear", symbol: str | None = None,
                               status: str | None = None) -> list[dict[str, Any]]:
        markets = await self._load()
        if symbol:
            chosen = [markets[symbol.upper()]] if symbol.upper() in markets else []
        else:
            chosen = list({m["symbol"]: m for m in markets.values()}.values())     # one row per market
        rows = []
        for m in chosen:
            cs = self._cs(m)
            step = self._step((m.get("precision") or {}).get("amount"))
            tick = self._step((m.get("precision") or {}).get("price"))
            limits = m.get("limits") or {}
            lo, hi = (limits.get("amount") or {}).get("min"), (limits.get("amount") or {}).get("max")
            cost_min = (limits.get("cost") or {}).get("min")
            lot = {"qtyStep": _num(step * cs) if step else "",
                   "minOrderQty": _num(Decimal(str(lo)) * cs) if lo else (_num(step * cs) if step else ""),
                   "maxOrderQty": _num(Decimal(str(hi)) * cs) if hi else "",
                   "minNotionalValue": _num(cost_min) if cost_min else ""}
            rows.append({"symbol": f"{m['base']}USDT", "status": "Trading", "lotSizeFilter": lot,
                         "priceFilter": {"tickSize": _num(tick) if tick else ""}})
        return rows

    async def get_ticker(self, symbol: str, category: str = "linear") -> dict[str, Any]:
        m = await self._market(symbol)
        t = await self._call("fetch_ticker", m["symbol"])
        last = t.get("last") or t.get("close")
        if not last:
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): no price for {symbol}")
        mark = t.get("markPrice") or (t.get("info") or {}).get("markPrice") or last
        return {"symbol": symbol.upper(), "lastPrice": _num(last), "markPrice": _num(mark),
                "bid1Price": _num(t.get("bid")), "ask1Price": _num(t.get("ask")),
                "volume24h": _num(t.get("baseVolume")), "turnover24h": _num(t.get("quoteVolume"))}

    async def _equity(self, bypass: bool = False) -> float:
        coin = self.profile.settle
        b = await self._call("fetch_balance", bypass=bypass)
        total = (b.get(coin) or {}).get("total")
        if total is None:
            total = (b.get("total") or {}).get(coin)
        if total is None:
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): no {coin} balance in the answer")
        equity = float(total)
        if not self.profile.balance_includes_upl:
            positions = await self._call("fetch_positions", bypass=bypass)
            equity += sum(_f(p.get("unrealizedPnl")) for p in positions or []
                          if self._rel(p.get("symbol")))
        return equity

    async def get_account_balance(self, coin: str = "USDT", bypass_breaker: bool = False) -> float:
        return await self._equity(bypass=bypass_breaker)

    async def get_balance_safe(self, coin: str = "USDT") -> tuple[float, str]:
        try:
            return await self._equity(), "wallet"
        except Exception as e:
            logger.warning(f"[{self.venue_id}] balance unreadable: {e}")
            return 0.0, "unavailable"

    async def get_usdt_trading_balance(self) -> tuple[float, str]:
        return await self.get_balance_safe()

    async def _resting_triggers(self, ccxt_symbol: str, bypass: bool = False) -> list[dict]:
        """The resting stop / take-profit / trailing orders on one symbol. Raises when they cannot
        be read: a stop that is there but unread would otherwise be placed a second time."""
        rows, seen = [], set()
        for params in self._listings:
            for o in await self._call("fetch_open_orders", ccxt_symbol, None, None, dict(params), bypass=bypass) or []:
                if o.get("id") not in seen:
                    seen.add(o.get("id"))
                    rows.append(o)
        return [o for o in rows if o.get("triggerPrice") or o.get("stopPrice") or o.get("stopLossPrice")
                or o.get("takeProfitPrice") or o.get("trailingPercent") or o.get("trailingAmount")]

    def _param_sets(self) -> list[dict]:
        """The parameter sets that reach a trigger order (to cancel or fetch one), in order."""
        out: list[dict] = []
        for params in (*self._cancel_params, self._trigger_params, *self._listings):
            if params not in out:
                out.append(dict(params))
        return out or [{}]

    async def _cancel_trigger(self, m: dict, order_id) -> None:
        """Cancel a resting trigger order by id, wherever the venue files it. Raises the last
        list's answer when none holds it (benign when the order is simply gone)."""
        last: Exception | None = None
        for params in self._param_sets():
            try:
                await self._call("cancel_order", order_id, m["symbol"], params, bypass=True)
                return
            except (BenignOrderStateError, VenueRejected) as e:
                last = e
        raise last

    async def _renew(self, m: dict, stop: dict, long: bool, amount: float) -> dict | None:
        """A stop of the relay's near the end of the venue's lifetime for it, placed again at its
        own price (by the usual replace: new before old on perpetuals, cancel-first on spot).
        Returns the stop that now rests, or None when the renewal failed (the position then
        reads as unprotected, and the stop audit re-arms it)."""
        if _tag_kind(self._dec(stop.get("clientOrderId"))) != _STOP:
            return stop                                    # not the relay's: not the relay's to renew
        px = _f(stop.get("stopLossPrice") or stop.get("triggerPrice") or stop.get("stopPrice"))
        r = await self._replace(m, _STOP, "sell" if long else "buy", amount, px, [stop],
                                self._price_param(_STOP, long, px))
        if r["retCode"] != 0:
            logger.error(f"[{self.venue_id}] {m['symbol']}: an expiring stop at {px} could not be renewed "
                         f"({r['retMsg']}); the stop audit re-arms it")
            return None
        logger.info(f"[{self.venue_id}] {m['symbol']}: stop at {px} renewed before the venue lets it expire")
        fresh = [o for o in await self._resting_triggers(m["symbol"], bypass=True) if not self._aged(o)]
        return self._stop_of(fresh, m["symbol"], long, 0)

    def _aged(self, o: dict) -> bool:
        """A trigger order past 80% of the venue's lifetime for it (renewed before it expires)."""
        life = self._max_age
        born = o.get("timestamp")
        return bool(life and born) and time.time() * 1000 - float(born) > 0.8 * life * 1000

    def _stop_of(self, triggers: list[dict], ccxt_symbol: str, long: bool, mark: float) -> dict | None:
        """The tightest stop order protecting this side (a reduce-only closing trigger below the
        price for a long, above it for a short)."""
        closing = "sell" if long else "buy"
        best = None
        for o in triggers:
            if o.get("symbol") != ccxt_symbol or o.get("side") != closing:
                continue
            if o.get("trailingPercent") or o.get("trailingAmount"):
                continue
            kind = _tag_kind(self._dec(o.get("clientOrderId")))
            px = _f(o.get("stopLossPrice") or o.get("triggerPrice") or o.get("stopPrice"))
            if kind == _TAKE_PROFIT or o.get("takeProfitPrice") or px <= 0:
                continue
            if kind != _STOP and not o.get("stopLossPrice") and mark > 0 and (px >= mark if long else px <= mark):
                continue                                   # a foreign trigger on the profit side
            if best is None or (px > best[0] if long else px < best[0]):
                best = (px, o)
        return best[1] if best else None

    async def get_positions(self, category: str = "linear", symbol: str | None = None,
                            settle_coin: str | None = None, limit: int = 200,
                            bypass_breaker: bool = False) -> list[dict[str, Any]]:
        await self._load()
        ccxt_symbol = (await self._market(symbol))["symbol"] if symbol else None
        raw = await self._call("fetch_positions", [ccxt_symbol] if ccxt_symbol else None, bypass=bypass_breaker)
        held = [p for p in raw or [] if self._rel(p.get("symbol")) and _f(p.get("contracts")) > 0]
        triggers: list[dict] = []
        for s in sorted({p["symbol"] for p in held}):
            triggers += await self._resting_triggers(s, bypass=bypass_breaker)
        out = []
        for p in held:
            rel = self._rel(p["symbol"])
            m = self._markets[rel]
            long = p.get("side") == "long"
            size = self._to_coins(m, p.get("contracts"))
            mark = _f(p.get("markPrice"))
            stop = self._stop_of(triggers, p["symbol"], long, mark)
            if stop is not None and self._aged(stop):
                stop = await self._renew(m, stop, long, _f(p.get("contracts")))
            covered = stop is not None and self._to_coins(m, stop.get("amount") or 0) >= size - Decimal("1e-12")
            closes_all = stop is not None and bool((stop.get("info") or {}).get("closePosition") in (True, "true"))
            stop_px = _f(stop.get("stopLossPrice") or stop.get("triggerPrice") or stop.get("stopPrice")) if stop else 0
            out.append({
                "symbol": rel, "side": "Buy" if long else "Sell", "size": _num(size),
                "avgPrice": _num(p.get("entryPrice")), "markPrice": _num(p.get("markPrice")),
                "unrealisedPnl": _num(p.get("unrealizedPnl")), "liqPrice": _num(p.get("liquidationPrice")),
                "leverage": _num(p.get("leverage")), "positionIdx": 0,
                "stopLoss": _num(stop_px) if stop is not None and (covered or closes_all) else "",
                "trailingStop": "", "createdTime": str(p.get("timestamp") or ""),
            })
        return out

    async def get_open_orders(self, symbol: str | None = None, category: str = "linear",
                              settle_coin: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        ccxt_symbol = (await self._market(symbol))["symbol"] if symbol else None
        plain = await self._call("fetch_open_orders", ccxt_symbol)
        triggers = await self._resting_triggers(ccxt_symbol) if ccxt_symbol else []
        seen, out = set(), []
        for o in [*(plain or []), *triggers]:
            if o.get("id") in seen:
                continue
            seen.add(o.get("id"))
            out.append(self._order_row(o))
        return out

    async def _find_by_link(self, ccxt_symbol: str, link: str) -> dict | None:
        cid = self._enc(link)
        try:
            return await self._call("fetch_order", None, ccxt_symbol, {"clientOrderId": cid})
        except VenueRejected:
            pass
        for method, capability in _LOOKUPS:
            if not self.x.has.get(capability):
                continue
            try:
                rows = await self._call(method, ccxt_symbol)
            except VenueRejected:
                continue
            for o in rows or []:
                if o.get("clientOrderId") == cid:
                    return o
        return None

    async def get_order(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None,
                        category: str = "linear") -> dict[str, Any] | None:
        m = await self._market(symbol)
        if order_id:
            try:
                o = await self._call("fetch_order", order_id, m["symbol"])
            except VenueRejected as e:
                if "order not found" not in str(e):
                    raise
                o = None
                for params in self._param_sets():
                    if not params:
                        continue
                    try:
                        o = await self._call("fetch_order", order_id, m["symbol"], params)
                        break
                    except VenueRejected as e2:
                        if "order not found" not in str(e2):
                            raise
                if o is None:
                    return None
        elif order_link_id:
            o = await self._find_by_link(m["symbol"], order_link_id)
        else:
            return None
        return self._order_row(o, m) if o else None

    async def get_order_history(self, category: str = "linear", symbol: str | None = None,
                                order_id: str | None = None, limit: int = 20,
                                order_link_id: str | None = None) -> list[dict[str, Any]]:
        if symbol and (order_id or order_link_id):
            one = await self.get_order(symbol, order_id=order_id, order_link_id=order_link_id)
            return [one] if one else []
        m = await self._market(symbol) if symbol else None
        rows = await self._call("fetch_closed_orders", m["symbol"] if m else None, None, limit)
        rows = sorted(rows or [], key=lambda o: o.get("timestamp") or 0, reverse=True)
        return [self._order_row(o) for o in rows if self._rel(o.get("symbol"))][:limit]

    async def get_closed_pnl(self, category: str = "linear", symbol: str | None = None, limit: int = 50,
                             start_time: int | None = None, end_time: int | None = None,
                             max_pages: int = 1) -> list[dict[str, Any]]:
        """Bybit's closed-PnL records, built from the venue's fills where a fill carries its
        realised PnL (Profile.realized_pnl_field): one record per order, newest first, with the
        order's side (the relay counts only the side that closes its position), its size in
        coins, its average price and its PnL less the order's fees. Fees paid in another coin
        than the settlement coin are not subtracted. Where fills carry no PnL: no records (the
        relay then prices from its own fills, and a close the venue made stays unpriced)."""
        field = self.profile.realized_pnl_field
        if not field or not self.profile.capabilities.closed_pnl or not symbol:
            return []
        m = await self._market(symbol)
        trades: list[dict] = []
        since = start_time
        for _ in range(max(1, max_pages)):
            page = await self._call("fetch_my_trades", m["symbol"], since, 100)
            page = [t for t in page or [] if t.get("id") not in {x.get("id") for x in trades}]
            if not page:
                break
            trades += page
            since = max(int(t.get("timestamp") or 0) for t in page) + 1
            if len(page) < 100:
                break
        orders: dict[str, list[dict]] = {}
        for t in trades:
            if end_time is not None and int(t.get("timestamp") or 0) > end_time:
                continue
            orders.setdefault(str(t.get("order") or t.get("id")), []).append(t)
        settle = self.profile.settle
        out = []
        for order_id, fills in orders.items():
            size = sum((self._to_coins(m, t.get("amount")) for t in fills), Decimal(0))
            if size <= 0:
                continue
            value = sum((self._to_coins(m, t.get("amount")) * Decimal(str(t.get("price") or 0)) for t in fills),
                        Decimal(0))
            pnls = [(t.get("info") or {}).get(field) for t in fills]
            fees = sum((Decimal(str((t.get("fee") or {}).get("cost") or 0)) for t in fills
                        if (t.get("fee") or {}).get("currency") in (settle, None)), Decimal(0))
            pnl = "" if any(x in (None, "") for x in pnls) else _num(sum(Decimal(str(x)) for x in pnls) - fees)
            last = max(int(t.get("timestamp") or 0) for t in fills)
            out.append({"orderId": order_id, "symbol": symbol.upper(),
                        "side": "Buy" if fills[0].get("side") == "buy" else "Sell",
                        "closedSize": _num(size), "avgExitPrice": _num(value / size), "closedPnl": pnl,
                        "createdTime": str(min(int(t.get("timestamp") or 0) for t in fills)),
                        "updatedTime": str(last)})
        out.sort(key=lambda r: int(r["updatedTime"]), reverse=True)
        return out[:limit * max(1, max_pages)]

    async def get_transaction_log(self, category: str = "linear", start_time: int | None = None,
                                  end_time: int | None = None, max_pages: int = 200) -> list[dict[str, Any]]:
        """Funding payments as Bybit's SETTLEMENT entries. Raises where the venue has no funding
        history (so the relay leaves funding unknown, never 0)."""
        if not self.profile.capabilities.funding_history or not self.x.has.get("fetchFundingHistory"):
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): not supported: funding history")
        await self._load()
        rows = await self._call("fetch_funding_history", None, start_time)
        out = []
        for r in rows or []:
            rel = self._rel(r.get("symbol"))
            ts = r.get("timestamp") or 0
            if rel and (end_time is None or ts <= end_time):
                out.append({"type": "SETTLEMENT", "symbol": rel, "change": _num(r.get("amount")),
                            "funding": _num(r.get("amount")), "transactionTime": str(ts)})
        return out

    # ----------------------------------------------------------------- orders
    async def place_market_order(self, symbol: str, side: str, qty: str, category: str = "linear",
                                 reduce_only: bool = False, close_on_trigger: bool = False,
                                 stop_loss: str | None = None, sl_trigger_by: str = "MarkPrice",
                                 order_link_id: str | None = None) -> dict[str, Any]:
        """A market order. `stop_loss` is not attached here: the relay sets the stop right after
        the fill (set_trading_stop), as a resting stop order."""
        m = await self._market(symbol)
        params: dict[str, Any] = {}
        if reduce_only:
            params["reduceOnly"] = True
        if order_link_id:
            params["clientOrderId"] = self._enc(order_link_id)
        ref = None
        if self.profile.market_price:
            t = await self._call("fetch_ticker", m["symbol"], bypass=reduce_only)
            ref = _f(t.get("last") or t.get("close")) or None
            if ref is None:
                raise VenueRejected(f"{REJECTED} ({self.venue_id}): no price for {symbol} to send a market order with")
        o = await self._call("create_order", m["symbol"], "market", side.lower(), self._to_amount(m, qty), ref,
                             params, bypass=reduce_only)
        return {"orderId": str(o.get("id") or ""), "orderLinkId": order_link_id or ""}

    async def place_limit_order(self, symbol: str, side: str, qty: str, price: str, category: str = "linear",
                                post_only: bool = True, reduce_only: bool = False, order_link_id: str | None = None,
                                stop_loss: str | None = None, sl_trigger_by: str = "MarkPrice") -> dict[str, Any]:
        if reduce_only and not self.profile.capabilities.reduce_only_limit:
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): not supported: resting reduce-only limit orders")
        m = await self._market(symbol)
        params: dict[str, Any] = {"postOnly": bool(post_only)}
        if reduce_only:
            params["reduceOnly"] = True
        if order_link_id:
            params["clientOrderId"] = self._enc(order_link_id)
        o = await self._call("create_order", m["symbol"], "limit", side.lower(), self._to_amount(m, qty),
                             float(price), params)
        return {"orderId": str(o.get("id") or ""), "orderLinkId": order_link_id or ""}

    async def amend_order(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None,
                          price: str | None = None, qty: str | None = None, category: str = "linear") -> dict:
        if not self.x.has.get("editOrder"):
            raise VenueRejected(f"{REJECTED} ({self.venue_id}): not supported: amending an order")
        m = await self._market(symbol)
        current = await self.get_order(symbol, order_id=order_id, order_link_id=order_link_id)
        if current is None:
            raise BenignOrderStateError(f"order not exists or too late ({self.venue_id})")
        new_qty = self._to_amount(m, qty if qty is not None else current["qty"])
        new_price = float(price) if price is not None else (_f(current["price"]) or None)
        await self._call("edit_order", current["orderId"], m["symbol"], "limit",
                         "buy" if current["side"] == "Buy" else "sell", new_qty, new_price)
        return {"orderId": current["orderId"]}

    async def cancel_order(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None,
                           category: str = "linear") -> dict[str, Any]:
        m = await self._market(symbol)
        if not order_id and order_link_id:
            found = await self._find_by_link(m["symbol"], order_link_id)
            if found is None:
                raise BenignOrderStateError(f"order not exists or too late ({self.venue_id})")
            order_id = found.get("id")
        try:
            await self._call("cancel_order", order_id, m["symbol"], bypass=True)
        except (BenignOrderStateError, VenueRejected) as first:
            if self._param_sets() == [{}]:
                raise
            # Not among the plain orders: a stop / take-profit, which this venue files apart (MEXC
            # answers "invalid order" for a plan order cancelled as a plain one).
            try:
                await self._cancel_trigger(m, order_id)
            except (BenignOrderStateError, VenueRejected):
                raise first from None
        return {"orderId": order_id}

    async def cancel_all_orders(self, category: str = "linear", symbol: str | None = None,
                                settle_coin: str | None = None, base_coin: str | None = None) -> dict[str, Any]:
        if symbol:
            symbols = [(await self._market(symbol))["symbol"]]
        else:
            held = await self._call("fetch_positions", None, bypass=True)
            resting = await self._call("fetch_open_orders", None, bypass=True)
            symbols = sorted({o.get("symbol") for o in [*(held or []), *(resting or [])] if self._rel(o.get("symbol"))})
        for s in symbols:
            sets = [{}] + ([self.profile.cancel_all_trigger_params] if self.profile.cancel_all_trigger_params
                           else [p for p in self._param_sets() if p])
            for params in sets:
                try:
                    await self._call("cancel_all_orders", s, dict(params), bypass=True)
                except VenueRejected as e:            # not offered (OKX, Hyperliquid): swept below
                    logger.info(f"[{self.venue_id}] {s}: cancel-all {params or ''} not available ({e})")
            # Whatever a cancel-all did not reach (or the venue has none): one by one.
            plain = await self._call("fetch_open_orders", s, bypass=True)
            for o in [*(plain or []), *await self._resting_triggers(s, bypass=True)]:
                try:
                    await self.cancel_order(self._rel(s), order_id=str(o["id"]))
                except BenignOrderStateError:
                    pass
                except VenueRejected as e:                # one refusal stops no other cancel
                    logger.warning(f"[{self.venue_id}] {s}: order {o.get('id')} not cancelled ({e})")
        return {"retCode": 0, "result": {"symbols": [self._rel(s) for s in symbols]}}

    # ----------------------------------------------------------------- leverage, margin
    async def set_leverage(self, symbol: str, leverage: float, category: str = "linear") -> dict[str, Any]:
        m = await self._market(symbol)
        try:
            await self._call("set_leverage", leverage, m["symbol"])
        except Exception as e:
            if _not_modified(str(e)):
                return {"retCode": 110043, "retMsg": "leverage not modified"}
            return {"retCode": -1, "retMsg": str(e)}
        return {"retCode": 0, "retMsg": "OK"}

    async def switch_isolated_margin(self, symbol: str, leverage: float, isolated: bool = True,
                                     category: str = "linear") -> dict[str, Any]:
        if not self.x.has.get("setMarginMode"):
            return {"retCode": -1, "retMsg": f"{self.venue_id} cannot set the margin mode through its API: leave "
                                             f"isolated_margin empty and set it on the exchange"}
        m = await self._market(symbol)
        try:
            await self._call("set_margin_mode", "isolated" if isolated else "cross", m["symbol"],
                             {"leverage": leverage})
        except Exception as e:
            if _not_modified(str(e)):
                return {"retCode": 110026, "retMsg": "margin mode not modified"}
            return {"retCode": -1, "retMsg": str(e)}
        return {"retCode": 0, "retMsg": "OK"}

    # ----------------------------------------------------------------- the stop
    async def set_trading_stop(self, symbol: str, stop_loss: float | None = None, take_profit: float | None = None,
                               tpsl_mode: str = "Full", tp_size: float | None = None, sl_size: float | None = None,
                               tp_order_type: str = "Market", sl_order_type: str = "Market", position_idx: int = 0,
                               category: str = "linear", sl_trigger_by: str = "MarkPrice",
                               tp_trigger_by: str = "MarkPrice", trailing_stop: float | None = None,
                               active_price: float | None = None) -> dict[str, Any]:
        """Bybit's position stop, as resting orders: the stop (stop_loss; 0 removes it), one
        take-profit (take_profit; 0 removes it) and a trailing stop (trailing_stop, a price
        distance; 0 removes it). Answers {retCode, retMsg} like Bybit; raises only when the
        exchange cannot be reached."""
        if not self.can_stop and any(v for v in (stop_loss, take_profit, trailing_stop)):
            return {"retCode": -1, "retMsg": f"{self.venue_id} cannot hold a stop or take-profit order "
                                             f"(nothing was sent: a plain order would close at market)"}
        if self._tags_echoed is False and any(v for v in (stop_loss, take_profit, trailing_stop)):
            return {"retCode": -1, "retMsg": f"{self.venue_id} does not give the relay's tag back on stop orders: "
                                             f"the relay cannot manage stops there"}
        try:
            m = await self._market(symbol)
            held = await self._holding(m)
        except VenueRejected as e:
            return {"retCode": -1, "retMsg": str(e)}
        if held is None:
            return {"retCode": 10001, "retMsg": "can not set tp/sl/ts for zero position"}
        long, amount, mark = held
        closing = "sell" if long else "buy"
        triggers = await self._resting_triggers(m["symbol"], bypass=True)
        results = []
        if stop_loss is not None:
            results.append(await self._replace(m, _STOP, closing, amount, stop_loss, triggers,
                                               self._price_param(_STOP, long, stop_loss)))
        if take_profit is not None:
            results.append(await self._replace(m, _TAKE_PROFIT, closing, amount, take_profit, triggers,
                                               self._price_param(_TAKE_PROFIT, long, take_profit)))
        if trailing_stop is not None:
            results.append(await self._trailing(m, closing, amount, trailing_stop, active_price, mark, triggers))
        failed = [r for r in results if r["retCode"] not in (0, 34040)]
        if failed:
            return failed[0]
        if results and all(r["retCode"] == 34040 for r in results):
            return {"retCode": 34040, "retMsg": "not modified"}
        return {"retCode": 0, "retMsg": "OK"}

    async def _holding(self, m: dict) -> tuple[bool, float, float] | None:
        """(long, amount in the exchange's units, mark price) of the position on this market."""
        positions = await self._call("fetch_positions", [m["symbol"]], bypass=True)
        held = [p for p in positions or [] if p.get("symbol") == m["symbol"] and _f(p.get("contracts")) > 0]
        if not held:
            return None
        return held[0].get("side") == "long", _f(held[0].get("contracts")), _f(held[0].get("markPrice"))

    def _price_param(self, kind: str, long: bool, price) -> dict:
        """How this venue is asked for a stop / take-profit at this price (profile.stop_style)."""
        if self.profile.stop_style == "direction":
            falling = (kind == _STOP) == long              # a long's stop, a short's take-profit
            return {"triggerPrice": float(price),
                    **self.profile.trigger_direction["falling" if falling else "rising"]}
        return {"stopLossPrice" if kind == _STOP else "takeProfitPrice": float(price)}

    def _own(self, triggers: list[dict], m: dict, kind: str) -> list[dict]:
        return [o for o in triggers if o.get("symbol") == m["symbol"]
                and _tag_kind(self._dec(o.get("clientOrderId"))) == kind]

    async def _replace(self, m: dict, kind: str, closing: str, amount: float, price, triggers: list[dict],
                       price_param: dict) -> dict[str, Any]:
        """Place the new resting order, then cancel this kind's old ones (never the other way
        round: the position is never left without its stop)."""
        old = self._own(triggers, m, kind)
        if float(price) == 0:
            return await self._cancel_all(m, old)
        if len(old) == 1:
            o = old[0]
            px = _f(o.get("stopLossPrice") if kind == _STOP else o.get("takeProfitPrice")) or _f(o.get("triggerPrice"))
            if abs(px - float(price)) <= 1e-12 and _f(o.get("amount")) >= amount - 1e-12 and not self._aged(o):
                return {"retCode": 34040, "retMsg": "not modified"}
        tag = _tag(kind)
        params = {**price_param, **self._closing, "clientOrderId": self._enc(tag), **self._trigger_extra,
                  **(self.profile.stop_params if kind == _STOP else {})}
        try:
            placed = await self._call("create_order", m["symbol"], "market", closing, amount,
                                      float(price) if self.profile.market_price else None, params, bypass=True)
        except (VenueRejected, ConnectionError) as e:
            if isinstance(e, ConnectionError) and await self._rests(m, tag):
                return await self._cancel_all(m, old)        # the answer was lost, the order was not
            return {"retCode": -1, "retMsg": str(e)}
        refused = await self._check_tag_echo(m, placed, tag)
        if refused:
            return refused
        return await self._cancel_all(m, old)

    async def _rests(self, m: dict, tag: str) -> bool:
        """Whether an order with this tag rests on the venue (after a lost answer: was it placed?)."""
        try:
            listed = await self._resting_triggers(m["symbol"], bypass=True)
        except Exception:
            return False
        return any(self._dec(o.get("clientOrderId")) == self._dec(self._enc(tag)) for o in listed)

    async def _check_tag_echo(self, m: dict, placed: dict | None, tag: str) -> dict | None:
        """At the first trigger order: does the venue give the relay's tag back? If it reads back
        without it, the order is cancelled again and stops are refused from then on."""
        if self._tags_echoed is not None or not isinstance(placed, dict) or not placed.get("id"):
            return None
        try:
            listed = await self._resting_triggers(m["symbol"], bypass=True)
        except Exception:
            return None                                    # unreadable now: checked at the next stop
        mine = [o for o in listed if str(o.get("id")) == str(placed["id"])]
        if not mine:
            return None                                    # not listed yet: checked at the next stop
        if self._dec(mine[0].get("clientOrderId")) == self._dec(self._enc(tag)):   # as the venue reads it back
            self._tags_echoed = True
            return None
        self._tags_echoed = False
        await self._cancel_all(m, mine)
        logger.error(f"[{self.venue_id}] the exchange does not give the relay's tag back on stop orders: stops are "
                     f"refused on this venue (the relay could not tell its own stop from another)")
        return {"retCode": -1, "retMsg": f"{self.venue_id} does not give the relay's tag back on stop orders"}

    async def _cancel_all(self, m: dict, orders: list[dict]) -> dict[str, Any]:
        problems = []
        for o in orders:
            try:
                await self._cancel_trigger(m, o["id"])
            except BenignOrderStateError:
                pass
            except Exception as e:
                problems.append(str(e))
        if problems:
            # The new order is in place; an old one still resting is a second, looser order.
            logger.warning(f"[{self.venue_id}] {m['symbol']}: an old resting order was not cancelled: {problems[0]}")
        return {"retCode": 0, "retMsg": "OK"}

    async def _trailing(self, m: dict, closing: str, amount: float, distance, active_price, mark: float,
                        triggers: list[dict]) -> dict[str, Any]:
        old = self._own(triggers, m, _TRAILING)
        if float(distance) == 0:
            return await self._cancel_all(m, old)
        if not self.profile.capabilities.trailing_stop or not self.x.has.get("createTrailingPercentOrder"):
            return {"retCode": -1, "retMsg": f"trailing stops are not supported on {self.venue_id}"}
        ref = float(active_price or mark or 0)
        if ref <= 0:
            return {"retCode": -1, "retMsg": "no price to turn the trailing distance into a percentage"}
        percent = float(distance) / ref * 100
        tag = _tag(_TRAILING)
        try:
            await self._call("create_trailing_percent_order", m["symbol"], "market", closing, amount, None, percent,
                             float(active_price) if active_price else None,
                             {**self._closing, "clientOrderId": self._enc(tag)}, bypass=True)
        except (VenueRejected, ConnectionError) as e:
            if not (isinstance(e, ConnectionError) and await self._rests(m, tag)):
                return {"retCode": -1, "retMsg": str(e)}
        return await self._cancel_all(m, old)


def _not_modified(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in ("not modified", "no need to change", "not changed", "same as", "already",
                                  "no change"))
