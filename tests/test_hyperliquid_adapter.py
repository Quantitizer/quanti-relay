"""HyperliquidVenueAdapter. Pure normalization + adapter behavior over SDK fakes (the SDK is
lazy-imported, so these run without it installed)."""
import asyncio

import relay.venue.hyperliquid_adapter as hl
from relay.venue.hyperliquid_adapter import (
    HyperliquidVenueAdapter,
    _normalize_instrument,
    _normalize_position,
    bybit_symbol,
    hl_coin,
)

# ---------------- pure symbol map + normalizers ----------------

def test_symbol_map_roundtrip():
    for sym in ("BTCUSDT", "ETHUSDT"):
        assert bybit_symbol(hl_coin(sym)) == sym
    assert hl_coin("1000PEPEUSDT") == "kPEPE" and bybit_symbol("kPEPE") == "1000PEPEUSDT"
    assert hl_coin("1000000BABYDOGEUSDT") == "kkBABYDOGE" and bybit_symbol("kkBABYDOGE") == "1000000BABYDOGEUSDT"


def test_normalize_position_long_short_and_stop():
    long = _normalize_position({"position": {"coin": "BTC", "szi": "0.5", "entryPx": "100"}}, stop_px=95.0)
    assert long == {"symbol": "BTCUSDT", "size": 0.5, "side": "Buy", "avgPrice": 100.0, "stopLoss": "95.0"}
    short = _normalize_position({"position": {"coin": "ETH", "szi": "-2", "entryPx": "50"}})
    assert short["side"] == "Sell" and short["size"] == 2.0 and short["stopLoss"] == ""   # no stop => has_stop False
    assert _normalize_position({"position": {"coin": "X", "szi": "0"}}) is None            # flat -> dropped


def test_normalize_instrument_bybit_shape():
    inst = _normalize_instrument({"name": "BTC", "szDecimals": 3}, {"dayNtlVlm": "1000000"})
    assert inst["symbol"] == "BTCUSDT" and inst["contractType"] == "LinearPerpetual"
    assert inst["quoteCoin"] == "USDT" and inst["symbolType"] == "" and inst["isPreListing"] is False
    assert inst["lotSizeFilter"]["qtyStep"] == "0.001" and inst["turnover24h"] == 1000000.0


# ---------------- adapter over SDK fakes ----------------

class _FakeExchange:
    def __init__(self):
        self.calls = []

    def market_open(self, coin, is_buy, sz, *a, **k):
        self.calls.append(("market_open", coin, is_buy, sz))
        return {"response": {"data": {"statuses": [{"filled": {"oid": 111, "avgPx": "62676.0", "totalSz": str(sz)}}]}}}

    def market_close(self, coin, sz=None, *a, **k):
        self.calls.append(("market_close", coin, sz))
        return {"response": {"data": {"statuses": [{"filled": {"oid": 333}}]}}}

    def order(self, coin, is_buy, sz, px, ot, reduce_only, *a, **k):
        self.calls.append(("order", coin, is_buy, sz, px, ot, reduce_only))
        return {"response": {"data": {"statuses": [{"resting": {"oid": 222}}]}}}


class _FakeInfo:
    def user_state(self, addr):
        return {"assetPositions": [{"position": {"coin": "BTC", "szi": "0.5", "entryPx": "100"}}],
                "marginSummary": {"accountValue": "1234.5"}}

    def frontend_open_orders(self, addr):
        return [{"coin": "BTC", "oid": 9, "reduceOnly": True, "triggerPx": "95",
                 "orderType": "Stop Market", "cloid": "rl-1"}]

    def spot_user_state(self, addr):
        return {"balances": [{"coin": "USDC", "total": "100.0"}, {"coin": "HYPE", "total": "0.0"}]}


def test_place_market_order_emulates_atomic_sl():
    ex = _FakeExchange()
    a = HyperliquidVenueAdapter(info=None, exchange=ex, address="0x", coin_map={})
    r = asyncio.run(a.place_market_order("BTCUSDT", "Buy", 0.5, stop_loss=95.0))
    assert r["orderId"] == "111" and r["avgPrice"] == "62676.0"     # real fill px, not hardcoded 0
    assert [c[0] for c in ex.calls] == ["market_open", "order"]   # entry, then a SEPARATE SL trigger
    sl = ex.calls[1]
    assert sl[1] == "BTC" and sl[2] is False and sl[6] is True    # opposite side (Sell), reduce_only
    assert sl[5]["trigger"]["tpsl"] == "sl" and sl[5]["trigger"]["triggerPx"] == 95.0


def test_reduce_only_uses_market_close_no_sl():
    ex = _FakeExchange()
    a = HyperliquidVenueAdapter(info=None, exchange=ex, address="0x")
    asyncio.run(a.place_market_order("BTCUSDT", "Sell", 0.5, reduce_only=True, stop_loss=95.0))
    assert [c[0] for c in ex.calls] == ["market_close"]           # close path, no SL trigger


def test_get_positions_synthesizes_stop_and_balance():
    a = HyperliquidVenueAdapter(info=_FakeInfo(), exchange=None, address="0x")
    pos = asyncio.run(a.get_positions())
    assert pos == [{"symbol": "BTCUSDT", "size": 0.5, "side": "Buy", "avgPrice": 100.0, "stopLoss": "95.0"}]
    assert asyncio.run(a.get_account_balance()) == 1334.5   # perp 1234.5 + spot USDC 100 (unified)
    assert a.pair_key("BTCUSDT") == "hyperliquid:BTCUSDT" and a.venue_id == "hyperliquid"


def test_hl_round_px_five_sig_figs():
    from relay.venue.hyperliquid_adapter import hl_round_px
    assert hl_round_px(54690.7) == 54691.0       # 6 sig figs -> 5 (HL rejects more precise prices)
    assert hl_round_px(77.24619) == 77.246
    assert hl_round_px(0) == 0 and hl_round_px(-5) == -5


def test_hl_leverage_safe_guards_short_liquidation():
    from relay.venue.hyperliquid_adapter import hl_leverage_safe, hl_liq_clearance
    # maxLev=5 => MMR 10% => at 5x a SHORT liquidates ~9.1% from entry, INSIDE a 10% SL
    ld, sr = hl_liq_clearance(5, 5)
    assert round(ld, 3) == 0.111 and round(sr, 3) == 0.091
    assert hl_leverage_safe(5, entry_leverage=5, sl_pct=0.10) is False   # short clearance < 10%
    assert hl_leverage_safe(10, entry_leverage=5, sl_pct=0.10) is True
    assert hl_leverage_safe(50, entry_leverage=5, sl_pct=0.10) is True
    assert hl_leverage_safe(5, entry_leverage=5, sl_pct=0.08) is True    # a tighter stop clears
    assert hl_leverage_safe(0, entry_leverage=5, sl_pct=0.10) is False
    assert hl_leverage_safe(None, entry_leverage=5, sl_pct=0.10) is False


def test_is_rate_limited():
    from relay.venue.hyperliquid_adapter import _is_rate_limited
    assert _is_rate_limited(Exception("(429, None, 'null', ...) Too Many Requests"))
    assert _is_rate_limited(Exception("rate limit exceeded"))
    assert not _is_rate_limited(Exception("insufficient margin"))


def test_run_retries_on_429_then_succeeds(monkeypatch):
    async def _no_sleep(*a, **k):
        pass
    monkeypatch.setattr(hl.asyncio, "sleep", _no_sleep)      # skip the real backoff/throttle waits
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("(429, None, 'null', None, {...}) Too Many Requests")
        return "ok"
    a = HyperliquidVenueAdapter(info=None, exchange=None, address="0x")
    assert asyncio.run(a._run(flaky)) == "ok"
    assert calls["n"] == 3                                   # two 429s retried, third succeeds


def test_run_reraises_non_rate_limit_error(monkeypatch):
    async def _no_sleep(*a, **k):
        pass
    monkeypatch.setattr(hl.asyncio, "sleep", _no_sleep)
    def boom():
        raise ValueError("insufficient margin")
    a = HyperliquidVenueAdapter(info=None, exchange=None, address="0x")
    try:
        asyncio.run(a._run(boom))
        assert False, "should have raised"
    except ValueError as e:
        assert "insufficient margin" in str(e)


def test_get_ticker_reads_l2_best_bid_ask():
    class _FI:
        def l2_snapshot(self, coin):
            return {"coin": coin, "levels": [[{"px": "100.0", "sz": "5"}], [{"px": "100.5", "sz": "3"}]]}
        def all_mids(self):
            return {"BTC": "100.25"}
    a = HyperliquidVenueAdapter(info=_FI(), exchange=None, address="0x")
    t = asyncio.run(a.get_ticker("BTCUSDT"))
    assert t["bid1Price"] == 100.0 and t["ask1Price"] == 100.5 and t["lastPrice"] == 100.25


def test_get_ticker_falls_back_to_mid_on_empty_book():
    class _FI:
        def l2_snapshot(self, coin):
            return {"levels": [[], []]}
        def all_mids(self):
            return {"BTC": "100.25"}
    a = HyperliquidVenueAdapter(info=_FI(), exchange=None, address="0x")
    t = asyncio.run(a.get_ticker("BTCUSDT"))
    assert t["bid1Price"] == "100.25" and t["ask1Price"] == "100.25"


def test_get_ticker_prefers_ws_bbo_cache():
    class _FI:
        def l2_snapshot(self, coin):
            raise AssertionError("must not REST when the WS BBO cache is warm")
        def all_mids(self):
            return {}
    a = HyperliquidVenueAdapter(info=_FI(), exchange=None, address="0x", ws_enabled=True)
    a._bbo_subs["BTC"] = 1                                    # already subscribed
    a._on_bbo({"data": {"coin": "BTC", "bbo": [{"px": "100.0"}, {"px": "100.5"}]}})
    t = asyncio.run(a.get_ticker("BTCUSDT"))
    assert t["bid1Price"] == 100.0 and t["ask1Price"] == 100.5 and t["lastPrice"] == 100.25


def test_get_ticker_ws_cold_lazily_subscribes_and_rest_fallbacks():
    subbed = {}
    class _FI:
        def subscribe(self, sub, cb):
            subbed["sub"] = sub
            return 7
        def l2_snapshot(self, coin):
            return {"levels": [[{"px": "9.0"}], [{"px": "9.1"}]]}
        def all_mids(self):
            return {}
    a = HyperliquidVenueAdapter(info=_FI(), exchange=None, address="0x", ws_enabled=True)
    t = asyncio.run(a.get_ticker("BTCUSDT"))
    assert subbed["sub"] == {"type": "bbo", "coin": "BTC"}    # lazy BBO subscribe on first touch
    assert t["bid1Price"] == 9.0 and t["ask1Price"] == 9.1    # REST l2 until the WS warms


def test_amend_order_modifies_resting_and_raises_on_reject():
    calls = {}
    class _FE:
        def modify_order(self, oid, coin, is_buy, sz, px, ot, ro):
            calls.update(oid=oid, coin=coin, is_buy=is_buy, sz=sz, px=px, ro=ro, tif=ot["limit"]["tif"])
            return {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
    class _FI:
        def frontend_open_orders(self, addr):
            return [{"oid": 42, "side": "A", "sz": "1.5", "reduceOnly": False}]
    a = HyperliquidVenueAdapter(info=_FI(), exchange=_FE(), address="0x")
    asyncio.run(a.amend_order("BTCUSDT", order_id=42, price="30001.7"))
    assert calls["oid"] == 42 and calls["coin"] == "BTC" and calls["is_buy"] is False
    assert calls["sz"] == 1.5 and calls["px"] == 30002.0 and calls["tif"] == "Alo"   # 5-sig-fig, post-only kept

    class _FErej:
        def modify_order(self, *a, **k):
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only order would cross"}]}}}
    a2 = HyperliquidVenueAdapter(info=_FI(), exchange=_FErej(), address="0x")
    try:
        asyncio.run(a2.amend_order("BTCUSDT", order_id=42, price="30001.7"))
        assert False, "should raise on rejection"
    except RuntimeError as e:
        assert "rejected" in str(e)

    class _FIgone:
        def frontend_open_orders(self, addr):
            return []
    a3 = HyperliquidVenueAdapter(info=_FIgone(), exchange=_FE(), address="0x")
    try:
        asyncio.run(a3.amend_order("BTCUSDT", order_id=99, price="30001.7"))
        assert False, "should raise when the order vanished"
    except RuntimeError as e:
        assert "no longer resting" in str(e)


def test_get_venue_adapter_routes_hyperliquid(monkeypatch):
    import types

    from relay.venue import adapter as ad
    sentinel = object()
    monkeypatch.setattr(hl, "build_hyperliquid_adapter", lambda va, contract_mode="linear": sentinel)
    va = types.SimpleNamespace(venue="hyperliquid", credentials_ref=None)
    assert ad.get_venue_adapter(va) is sentinel
