"""NOT WIRED YET: the running relay does not use this module. It is kept for the venue-interface phase (multi-venue), and reviewed and fixed when it is connected (tests/test_unwired_modules.py keeps the running relay from importing it before then).

HyperliquidVenueAdapter — trade on Hyperliquid through the same VenueAdapter seam as Bybit.

The seam is a PASS-THROUGH (callers read raw Bybit response fields), so this adapter NORMALIZES
Hyperliquid's `hyperliquid-python-sdk` responses INTO Bybit shapes. The module-level `_normalize_*`
helpers + the symbol map are PURE (no SDK import) and unit-tested; the adapter methods wrap the
(synchronous) SDK `Info`/`Exchange` in an executor. The SDK + `eth_account` are imported LAZILY (only
in `build_hyperliquid_adapter`), so importing this module never requires the SDK — the Bybit path is
untouched even when the SDK isn't installed.

Two Hyperliquid facts drive the design:
  * No atomic native stop-loss: `place_market_order(stop_loss=...)` places the entry AND a separate
    reduce-only trigger order (`tpsl='sl'`); `get_positions` synthesizes `stopLoss` from the resting
    trigger, so the caller's SL-verify-and-flatten check sees the stop exactly as on Bybit.
  * Symbols are coins (`BTC`, not `BTCUSDT`); `1000X` names are `kX` (e.g. `1000PEPE`->`kPEPE`).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time

from relay.util.symbols import construct_pair_id

logger = logging.getLogger(__name__)


def _is_rate_limited(e) -> bool:
    """HL (esp. testnet behind CloudFront) returns HTTP 429 on bursts — the SDK surfaces it as an
    error whose text carries the status. Retry those; never retry a real rejection."""
    s = str(e)
    return "429" in s or "Too Many Requests" in s or "rate limit" in s.lower()


# --------------------------------------------------------------------- symbol map (pure)
def hl_coin(symbol: str, coin_map: dict | None = None) -> str:
    """Bybit symbol -> Hyperliquid coin. 'BTCUSDT'->'BTC'; '1000PEPEUSDT'->'kPEPE'. A `coin_map`
    (built from HL `meta`) overrides the heuristic for names that don't follow the k-rule."""
    s = symbol.upper()
    if coin_map and symbol in coin_map:
        return coin_map[symbol]
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q):
            s = s[: -len(q)]
            break
    if s.startswith("1000000"):
        return "kk" + s[7:]
    if s.startswith("1000"):
        return "k" + s[4:]
    return s


def bybit_symbol(coin: str, quote: str = "USDT") -> str:
    """Hyperliquid coin -> Bybit-style symbol. 'BTC'->'BTCUSDT'; 'kPEPE'->'1000PEPEUSDT'."""
    c = coin
    if c.startswith("kk"):
        c = "1000000" + c[2:]
    elif c.startswith("k"):
        c = "1000" + c[1:]
    return f"{c}{quote}"


# --------------------------------------------------------------------- normalizers (pure)
def _normalize_position(asset_pos: dict, stop_px: float | None = None) -> dict | None:
    """HL clearinghouseState assetPosition -> Bybit position dict. `stop_px` = the resting reduce-only
    SL trigger price (from open_orders), synthesizing the Bybit inline `stopLoss`."""
    p = (asset_pos or {}).get("position", asset_pos or {})
    szi = float(p.get("szi") or 0)
    if szi == 0:
        return None
    return {
        "symbol": bybit_symbol(str(p.get("coin"))),
        "size": abs(szi),
        "side": "Buy" if szi > 0 else "Sell",           # Bybit vocabulary
        "avgPrice": float(p.get("entryPx") or 0),
        "stopLoss": (str(stop_px) if stop_px else ""),   # "" => caller sees has_stop=False
    }


def _normalize_instrument(u: dict, ctx: dict | None = None) -> dict:
    """HL meta universe item (+ optional asset ctx) -> Bybit instrument dict. qtyStep from szDecimals;
    synthesizes the Bybit instrument fields so instrument-filtering code works unchanged."""
    sz_dec = int(u.get("szDecimals") or 0)
    step = 10 ** (-sz_dec) if sz_dec > 0 else 1.0
    turnover = float((ctx or {}).get("dayNtlVlm") or 0)
    return {
        "symbol": bybit_symbol(str(u.get("name"))),
        "lotSizeFilter": {"qtyStep": str(step), "minOrderQty": str(step), "minNotionalValue": "10"},
        "priceFilter": {"tickSize": ""},                 # HL rounds px to 5 sig-figs; tick derived at format time
        "contractType": "LinearPerpetual", "quoteCoin": "USDT", "symbolType": "",
        "isPreListing": False, "launchTime": 0,
        "turnover24h": turnover,                          # convenience for liquidity filters
        "maxLeverage": int(u.get("maxLeverage") or 0),    # HL per-coin cap -> maint margin -> liq clearance
    }


def hl_round_px(px: float) -> float:
    """Round a price to Hyperliquid's rule: at most 5 significant figures (perps). HL REJECTS more
    precise prices, so an un-rounded SL-trigger price (the adapter has no real tickSize to format
    against) is rejected — the entry then lands NAKED and must be flattened. 5 sig figs is
    always valid; the finer 6-szDecimals decimal cap only tightens further and isn't needed here."""
    if not px or px <= 0:
        return px
    import math
    return round(px, max(0, 4 - math.floor(math.log10(abs(px)))))


def hl_liq_clearance(leverage: float, max_leverage: float) -> tuple[float, float]:
    """(long_drop, short_rise): the fractional price move from entry at which an ISOLATED position
    liquidates on Hyperliquid. HL maintenance margin = half the initial margin at max leverage =
    1/(2*maxLev). A long liquidates BELOW entry, a short ABOVE — and the short side is tighter."""
    m = 1.0 / (2.0 * max_leverage)
    long_drop = 1.0 - (1.0 - 1.0 / leverage) / (1.0 - m)
    short_rise = (1.0 + 1.0 / leverage) / (1.0 + m) - 1.0
    return long_drop, short_rise


def hl_leverage_safe(max_leverage, *, entry_leverage: float, sl_pct: float,
                     buffer: float = 0.0) -> bool:
    """True iff BOTH long and short liquidation clear the SL (+buffer) at entry_leverage on a coin with
    this max leverage. HL's high maintenance margin on low-max-leverage coins can put liquidation INSIDE
    a stop that would be safe on Bybit: e.g. a maxLev=5 coin has MMR 10%, so a SHORT at 5x liquidates
    ~9.1% from entry — inside a 10% stop. Such coins must be refused so the stop fires first."""
    try:
        mx = float(max_leverage)
    except (TypeError, ValueError):
        return False
    if mx <= 0:
        return False
    long_drop, short_rise = hl_liq_clearance(min(float(entry_leverage), mx), mx)
    return min(long_drop, short_rise) >= sl_pct + buffer


def _normalize_fill(f: dict) -> dict:
    """HL user_fill -> Bybit closed-pnl row (updatedTime/symbol/cumEntryValue/closedPnl)."""
    px = float(f.get("px") or 0)
    sz = float(f.get("sz") or 0)
    return {"updatedTime": f.get("time"), "symbol": bybit_symbol(str(f.get("coin"))),
            "cumEntryValue": px * sz, "closedPnl": float(f.get("closedPnl") or 0)}


def _extract_oid(resp) -> str | None:
    """Pull the order id out of an Exchange.order/market_open response."""
    try:
        st = resp["response"]["data"]["statuses"][0]
        return str(st.get("resting", st.get("filled", {})).get("oid"))
    except (KeyError, IndexError, TypeError):
        return None


def _order_rejected(resp) -> str | None:
    """Rejection reason from an order/modify response ('error' status or top-level 'err'), else None.
    Used to RAISE on a post-only would-cross so the SmartExecutor replaces instead of chasing a dead oid."""
    if isinstance(resp, dict) and resp.get("status") == "err":
        return str(resp.get("response"))
    try:
        st = resp["response"]["data"]["statuses"][0]
        if isinstance(st, dict) and st.get("error"):
            return str(st["error"])
    except (KeyError, IndexError, TypeError):
        pass
    return None


def _extract_fill_px(resp) -> str:
    """Average fill price of a FILLED market order (statuses[0].filled.avgPx). HL's order response — unlike
    Bybit's — carries the fill price only here; without it the caller sees avgPrice=0 and reads a real fill
    as '100% slippage' and aborts. '0' when the order didn't fill (e.g. resting)."""
    try:
        st = resp["response"]["data"]["statuses"][0]
        return str(st.get("filled", {}).get("avgPx") or "0")
    except (KeyError, IndexError, TypeError):
        return "0"


# --------------------------------------------------------------------- adapter
class HyperliquidVenueAdapter:
    venue_id = "hyperliquid"

    def __init__(self, info, exchange, address: str, coin_map: dict | None = None,
                 contract_mode: str = "linear", venue_account=None,
                 min_interval: float = 0.5, max_concurrency: int = 1, ws_enabled: bool = False):
        self._info = info            # SDK Info (reads) — or a fake in tests
        self._exchange = exchange    # SDK Exchange (signed writes, vault_address set) — or a fake
        self._address = address      # account/vault address for reads
        self._coin_map = coin_map or {}
        self.contract_mode = contract_mode
        self.venue_account = venue_account
        # WS best-bid/offer cache for the maker-chase touch (avoids a REST l2 read per chase).
        self._ws_enabled = bool(ws_enabled)
        self._bbo: dict[str, tuple] = {}          # coin -> (bid, ask), updated by the WS callback thread
        self._bbo_subs: dict[str, int | None] = {}  # coin -> subscription id (None = subscribe failed)
        # HL rate-limits hard (CloudFront throttles datacenter IPs especially) and a book rebalance fires
        # bursts of calls. Cap concurrency + serialize a min-interval so the burst doesn't 429, and retry
        # any residual 429 with exponential backoff. Both are tunable (build_* reads settings) — raise
        # hyperliquid_min_interval_s if a server IP still 429-storms.
        self._sema = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._throttle = asyncio.Lock()
        self._min_interval = float(min_interval)
        self._last_call = 0.0

    def pair_key(self, symbol: str) -> str:
        return construct_pair_id(self.venue_id, symbol)

    async def _run(self, fn, *args, **kwargs):
        call = functools.partial(fn, *args, **kwargs)
        loop = asyncio.get_event_loop()
        async with self._sema:
            for attempt in range(4):
                async with self._throttle:                     # min-interval spacing across all calls
                    wait = self._min_interval - (time.monotonic() - self._last_call)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last_call = time.monotonic()
                try:
                    return await loop.run_in_executor(None, call)
                except Exception as e:
                    if _is_rate_limited(e) and attempt < 3:
                        backoff = min(0.5 * (2 ** attempt), 8.0)
                        logger.warning("[hl] 429 rate-limited, retry %d in %.1fs", attempt + 1, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    raise

    def _coin(self, symbol: str) -> str:
        return hl_coin(symbol, self._coin_map)

    # ---- resting reduce-only SL trigger price per coin (for get_positions synthesis) ----
    async def _sl_px_by_coin(self) -> dict[str, float]:
        orders = await self._run(self._info.frontend_open_orders, self._address)
        out: dict[str, float] = {}
        for o in orders or []:
            # The relay's only resting reduce-only trigger per coin is its SL, so any reduce-only trigger
            # for the coin is the synthesized stop (don't depend on the exact orderType string HL returns).
            if o.get("reduceOnly") and o.get("triggerPx"):
                try:
                    out[str(o.get("coin"))] = float(o.get("triggerPx"))
                except (TypeError, ValueError):
                    pass
        return out

    # ---- writes ----
    async def place_market_order(self, symbol, side, qty, category=None, reduce_only=False,
                                 stop_loss=None, sl_trigger_by=None) -> dict:
        coin = self._coin(symbol)
        is_buy = str(side) == "Buy"
        if reduce_only:
            resp = await self._run(self._exchange.market_close, coin, float(qty))
        else:
            resp = await self._run(self._exchange.market_open, coin, is_buy, float(qty))
        oid = _extract_oid(resp)
        if stop_loss and not reduce_only:                # emulate Bybit's atomic SL: separate trigger
            sl_px = hl_round_px(float(stop_loss))        # 5-sig-fig or HL rejects -> naked position
            ot = {"trigger": {"isMarket": True, "triggerPx": sl_px, "tpsl": "sl"}}
            await self._run(self._exchange.order, coin, (not is_buy), float(qty), sl_px, ot, True)
        return {"orderId": oid, "avgPrice": _extract_fill_px(resp)}

    async def place_limit_order(self, symbol, side, qty, price, category=None, post_only=True,
                                reduce_only=False, order_link_id=None, stop_loss=None, sl_trigger_by=None) -> dict:
        coin = self._coin(symbol)
        ot = {"limit": {"tif": "Alo" if post_only else "Gtc"}}
        resp = await self._run(self._exchange.order, coin, str(side) == "Buy", float(qty),
                               hl_round_px(float(price)), ot, reduce_only)
        oid = _extract_oid(resp)
        if stop_loss and not reduce_only:
            sl_px = hl_round_px(float(stop_loss))
            sot = {"trigger": {"isMarket": True, "triggerPx": sl_px, "tpsl": "sl"}}
            await self._run(self._exchange.order, coin, (str(side) != "Buy"), float(qty), sl_px, sot, True)
        return {"orderId": oid}

    async def cancel_order(self, symbol, order_id=None, order_link_id=None, category=None) -> dict:
        return await self._run(self._exchange.cancel, self._coin(symbol), int(order_id))

    async def amend_order(self, symbol, order_id=None, price=None, category=None, **kw) -> dict:
        """Re-price a resting post-only order (SmartExecutor maker-chase). HL's modify_order needs the
        full order spec, so look the order up by oid for its side/remaining-size, then modify at the new
        5-sig-fig price keeping post-only (Alo). RAISES on a would-cross rejection (or a vanished order)
        so `_sx_amend` returns False and the SmartExecutor replaces rather than chasing a dead oid."""
        coin = self._coin(symbol)
        orders = await self._run(self._info.frontend_open_orders, self._address) or []
        o = next((x for x in orders if str(x.get("oid")) == str(order_id)), None)
        if o is None:
            raise RuntimeError(f"amend: order {order_id} no longer resting")
        is_buy = str(o.get("side")) == "B"                       # HL side: 'B' bid / 'A' ask
        sz = float(o.get("sz") or o.get("origSz") or 0)          # remaining open size
        resp = await self._run(self._exchange.modify_order, int(order_id), coin, is_buy, sz,
                               hl_round_px(float(price)), {"limit": {"tif": "Alo"}}, bool(o.get("reduceOnly")))
        rej = _order_rejected(resp)
        if rej:
            raise RuntimeError(f"amend rejected: {rej}")
        return resp

    async def set_trading_stop(self, symbol, stop_loss=None, category=None, sl_trigger_by=None, **kw) -> dict:
        """REPLACE the position's reduce-only SL trigger at a new price (no inline SL on HL). Cancels any
        existing reduce-only trigger(s) for the coin first — HL's `order` only ADDS, so without this a
        re-anchor accumulates duplicate stops (seen in production: ~4 triggers/symbol)."""
        coin = self._coin(symbol)
        pos = await self._position_raw(coin)
        if not pos:
            return {"retCode": 0}
        szi = float(pos.get("szi") or 0)
        is_long = szi > 0
        for o in (await self._run(self._info.frontend_open_orders, self._address) or []):
            if str(o.get("coin")) == coin and o.get("reduceOnly") and o.get("triggerPx"):
                try:
                    await self._run(self._exchange.cancel, coin, int(o.get("oid")))
                except Exception as e:
                    logger.warning("[hl] set_trading_stop: cancel stale trigger %s failed: %s", o.get("oid"), e)
        sl_px = hl_round_px(float(stop_loss))
        ot = {"trigger": {"isMarket": True, "triggerPx": sl_px, "tpsl": "sl"}}
        return await self._run(self._exchange.order, coin, (not is_long), abs(szi), sl_px, ot, True)

    async def set_leverage(self, symbol, leverage, category=None) -> dict:
        return await self._run(self._exchange.update_leverage, int(leverage), self._coin(symbol), False)

    # ---- reads ----
    async def _position_raw(self, coin: str) -> dict | None:
        state = await self._run(self._info.user_state, self._address)
        for ap in (state or {}).get("assetPositions", []):
            p = ap.get("position", {})
            if str(p.get("coin")) == coin and float(p.get("szi") or 0) != 0:
                return p
        return None

    async def get_positions(self, category=None, symbol=None, **kw) -> list:
        state = await self._run(self._info.user_state, self._address)
        sl_map = await self._sl_px_by_coin()
        out = []
        want = self._coin(symbol) if symbol else None
        for ap in (state or {}).get("assetPositions", []):
            coin = str(ap.get("position", {}).get("coin"))
            if want and coin != want:
                continue
            row = _normalize_position(ap, sl_map.get(coin))
            if row:
                out.append(row)
        return out

    async def get_open_orders(self, symbol=None, category=None, settle_coin=None, **kw) -> list:
        orders = await self._run(self._info.frontend_open_orders, self._address)
        return [{"symbol": bybit_symbol(str(o.get("coin"))), "orderId": str(o.get("oid")),
                 "orderLinkId": str(o.get("cloid") or "")} for o in (orders or [])]

    async def get_closed_pnl(self, category=None, symbol=None, limit=50, start_time=None, end_time=None) -> list:
        fills = await self._run(self._info.user_fills, self._address)
        rows = [_normalize_fill(f) for f in (fills or []) if float(f.get("closedPnl") or 0) != 0]
        if start_time:
            rows = [r for r in rows if (r.get("updatedTime") or 0) >= start_time]
        return rows[:limit]

    async def get_all_tickers(self, category=None, symbol=None) -> dict:
        meta, ctxs = await self._run(self._info.meta_and_asset_ctxs)
        out = {}
        for u, ctx in zip(meta.get("universe", []), ctxs):
            out[bybit_symbol(str(u.get("name")))] = {"turnover24h": float(ctx.get("dayNtlVlm") or 0),
                                                      "lastPrice": ctx.get("markPx")}
        return out

    def _on_bbo(self, msg) -> None:
        """WS BBO callback (runs on the SDK's WS thread). Cache the coin's best (bid, ask). A single dict
        assignment is atomic under the GIL, so the async reader needs no lock."""
        try:
            data = (msg or {}).get("data") or {}
            coin = data.get("coin")
            bbo = data.get("bbo") or []
            bid = float(bbo[0]["px"]) if (len(bbo) > 0 and bbo[0]) else None
            ask = float(bbo[1]["px"]) if (len(bbo) > 1 and bbo[1]) else None
            if coin and (bid or ask):
                self._bbo[str(coin)] = (bid, ask)
        except (KeyError, IndexError, TypeError, ValueError):
            pass

    def _ensure_bbo_sub(self, coin: str) -> None:
        if coin in self._bbo_subs:
            return
        try:
            self._bbo_subs[coin] = self._info.subscribe({"type": "bbo", "coin": coin}, self._on_bbo)
        except Exception as e:
            logger.warning("[hl] bbo subscribe %s failed (%s) — REST touch fallback", coin, e)
            self._bbo_subs[coin] = None

    async def close(self) -> None:
        """Tear down the WS so per-rebalance adapters don't leak connections. No-op when WS off."""
        if self._ws_enabled:
            try:
                self._info.disconnect_websocket()
            except Exception as e:
                logger.warning("[hl] ws disconnect failed: %s", e)

    async def get_ticker(self, symbol, category=None) -> dict:
        """Best bid/ask for the maker-chase touch. Prefers the WS BBO cache (no REST per touch);
        falls back to a REST l2 read (WS off, or before the first WS update lands), then to mid."""
        coin = self._coin(symbol)
        if self._ws_enabled:
            self._ensure_bbo_sub(coin)
            cached = self._bbo.get(coin)
            if cached and (cached[0] or cached[1]):
                bid, ask = cached
                last = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                return {"bid1Price": bid, "ask1Price": ask, "lastPrice": last}
        try:
            book = await self._run(self._info.l2_snapshot, coin)
            levels = (book or {}).get("levels") or []
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []
            bid = float(bids[0]["px"]) if bids else None          # best bid (levels sorted best-first)
            ask = float(asks[0]["px"]) if asks else None          # best ask
            if bid or ask:
                last = (bid + ask) / 2 if (bid and ask) else (bid or ask)
                return {"bid1Price": bid, "ask1Price": ask, "lastPrice": last}
        except Exception as e:
            logger.warning("[hl] l2 snapshot %s failed (%s) — falling back to mid", coin, e)
        mid = (await self._run(self._info.all_mids)).get(coin)
        return {"bid1Price": mid, "ask1Price": mid, "lastPrice": mid}

    async def list_instruments(self, category=None, symbol=None, status=None) -> list:
        meta, ctxs = await self._run(self._info.meta_and_asset_ctxs)
        rows = []
        for u, ctx in zip(meta.get("universe", []), ctxs):
            if symbol and bybit_symbol(str(u.get("name"))) != symbol:
                continue
            rows.append(_normalize_instrument(u, ctx))
        return rows

    async def get_account_balance(self, account_type=None, coin="USDT") -> float:
        """Total USDC equity = perp accountValue + spot USDC. Under Hyperliquid's UNIFIED account the
        perp clearinghouse reports accountValue=0 while flat and the collateral sits in spot USDC (which
        backs perp trading directly), so reading only marginSummary would under-report equity to 0 and
        size every order to nothing. Summing both gives the true collateral in every state."""
        perp = 0.0
        try:
            st = await self._run(self._info.user_state, self._address)
            perp = float(st["marginSummary"]["accountValue"])
        except (KeyError, TypeError, ValueError):
            pass
        spot = 0.0
        try:
            sp = await self._run(self._info.spot_user_state, self._address)
            for b in (sp.get("balances") or []):
                if str(b.get("coin")) == "USDC":
                    spot = float(b.get("total") or 0)
                    break
        except Exception:
            pass
        return perp + spot

    async def get_balance_safe(self, coin="USDT") -> tuple:
        return (await self.get_account_balance(), "hyperliquid")

    async def get_usdt_trading_balance(self) -> tuple:
        return (await self.get_account_balance(), "hyperliquid")

    def get_circuit_breaker_status(self) -> dict:
        return {"state": "closed", "venue": "hyperliquid"}


# --------------------------------------------------------------------- factory (lazy SDK)
def _resolve_private_key(venue_account) -> str:
    """`credentials_ref` -> agent-wallet private key from env. NULL ref => env HYPERLIQUID_AGENT_PRIVATE_KEY."""
    import os
    ref = getattr(venue_account, "credentials_ref", None)
    if ref:
        key = os.getenv(f"HYPERLIQUID_KEY_{ref}") or os.getenv(str(ref))
        if key:
            return key
    key = os.getenv("HYPERLIQUID_AGENT_PRIVATE_KEY")
    if not key:
        raise RuntimeError("No Hyperliquid agent private key (set HYPERLIQUID_AGENT_PRIVATE_KEY or credentials_ref)")
    return key


def build_hyperliquid_adapter(venue_account, contract_mode: str = "linear") -> HyperliquidVenueAdapter:
    """Construct the adapter from the venue account's agent wallet. SDK + eth_account are imported HERE
    only, so the module never requires them. Points at testnet/mainnet per hyperliquid_testnet, sets
    `vault_address` (None ⇒ plain account), and builds the symbol map from HL `meta`."""
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    from relay.config import settings

    wallet = Account.from_key(_resolve_private_key(venue_account))
    va_testnet = getattr(venue_account, "testnet", None)
    testnet = bool(va_testnet if va_testnet is not None else settings.testnet)
    base = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
    account_address = getattr(settings, "hyperliquid_account_address", None) or wallet.address
    vault_address = getattr(settings, "hyperliquid_vault_address", None) or None  # "" => plain account
    use_ws = bool(getattr(settings, "hyperliquid_use_ws", False))   # WS BBO for the maker-chase touch
    info = Info(base, skip_ws=(not use_ws))
    exchange = Exchange(wallet, base, account_address=account_address, vault_address=vault_address)
    coin_map = {bybit_symbol(u["name"]): u["name"] for u in (info.meta().get("universe", []))}
    return HyperliquidVenueAdapter(info, exchange, address=(vault_address or account_address),
                                   coin_map=coin_map, contract_mode=contract_mode, venue_account=venue_account,
                                   min_interval=float(getattr(settings, "hyperliquid_min_interval_s", 0.5)),
                                   max_concurrency=int(getattr(settings, "hyperliquid_max_concurrency", 1)),
                                   ws_enabled=use_ws)
