"""Safety-lane fixes for _with_timestamp_retry: never return None; reduce-only bypass."""
import asyncio

import pytest

from relay.exchange.bybit.rest_client import BybitRestClient


class _TS(Exception):
    pass


class _RL(Exception):
    pass


def _client(monkeypatch):
    c = BybitRestClient(api_key="k", api_secret="s", demo=True)
    monkeypatch.setattr(c, "_is_timestamp_error", lambda e: isinstance(e, _TS))
    monkeypatch.setattr(c, "_is_rate_limit_error", lambda e: isinstance(e, _RL))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(c, "sync_time", _noop)
    monkeypatch.setattr(asyncio, "sleep", _noop)
    return c


def test_retry_never_returns_none(monkeypatch):
    # attempts 0-2 = rate-limit (resynced stays False), attempt 3 = timestamp -> old code hit `continue`
    # on the final iteration and fell through to an implicit `return None`. Must now RAISE instead.
    c = _client(monkeypatch)
    n = {"i": 0}

    async def coro():
        i = n["i"]; n["i"] += 1
        raise (_RL() if i < 3 else _TS())

    with pytest.raises(Exception):
        asyncio.run(c._with_timestamp_retry(coro))
    assert n["i"] == 4   # all four attempts consumed, then raised (not returned None)


def test_success_returns_value(monkeypatch):
    c = _client(monkeypatch)

    async def coro():
        return {"ok": 1}

    assert asyncio.run(c._with_timestamp_retry(coro)) == {"ok": 1}


def test_bypass_throttle_still_returns(monkeypatch):
    # The reduce-only safety lane skips the semaphore but must behave identically on success.
    c = _client(monkeypatch)

    async def coro():
        return "closed"

    assert asyncio.run(c._with_timestamp_retry(coro, bypass_throttle=True)) == "closed"


def test_prices_are_sent_as_plain_decimals():
    from relay.exchange.bybit.rest_client import _num_str
    assert _num_str(0.00001234) == "0.00001234" and _num_str(0.1 + 0.2) == "0.3"
    assert _num_str(95.0) == "95" and _num_str("95.10") == "95.10"


def test_closed_pnl_follows_the_cursor_across_pages():
    import asyncio
    import time

    import httpx

    from relay.exchange.bybit.rest_client import BybitRestClient
    pages = {None: ([{"orderId": "a"}], "c2"), "c2": ([{"orderId": "b"}], "c3"), "c3": ([{"orderId": "c"}], "")}

    def handler(request):
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "result": {"timeNano": str(time.time_ns())}})
        rows, nxt = pages[request.url.params.get("cursor")]
        return httpx.Response(200, json={"retCode": 0, "result": {"list": rows, "nextPageCursor": nxt}})

    c = BybitRestClient(api_key="k", api_secret="s", demo=True)
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=c.base_url)
    got = asyncio.run(c.get_closed_pnl(symbol="BTCUSDT", max_pages=5))
    assert [r["orderId"] for r in got] == ["a", "b", "c"]
    assert [r["orderId"] for r in asyncio.run(c.get_closed_pnl(symbol="BTCUSDT"))] == ["a"]   # default: one page


def _mock_client(handler):
    import time

    import httpx

    from relay.exchange.bybit.rest_client import BybitRestClient

    def wrapped(request):
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "result": {"timeNano": str(time.time_ns())}})
        return handler(request)

    c = BybitRestClient(api_key="k", api_secret="s", demo=True)
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), base_url=c.base_url)
    return c


def test_timeouts_and_server_errors_are_counted(monkeypatch):
    import asyncio

    import httpx

    from relay.monitoring import metrics
    monkeypatch.setattr(metrics, "_bybit_failures", {"timeout": 0, "5xx": 0, "circuit_breaker_open": 0})

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def server_error(request):
        return httpx.Response(503, text="unavailable")

    for handler in (timeout, server_error):
        try:
            asyncio.run(_mock_client(handler).get_positions(symbol="BTCUSDT"))
        except Exception:
            pass
    counts = metrics.get_bybit_failures()
    assert counts["timeout"] >= 1 and counts["5xx"] >= 1


def test_equity_is_not_the_wallet_alone_when_unrealised_pnl_is_missing():
    import asyncio

    import httpx

    def handler(request):
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [
            {"totalEquity": "0", "totalMarginBalance": "", "totalWalletBalance": "1000", "totalPerpUPL": ""}]}})

    equity, source = asyncio.run(_mock_client(handler).get_balance_safe())
    assert (equity, source) == (0.0, "unavailable")              # unknown, not an overstated 1,000


def test_a_rejected_cancel_all_raises():
    import asyncio

    import httpx
    import pytest

    def handler(request):
        return httpx.Response(200, json={"retCode": 10001, "retMsg": "settleCoin invalid"})

    with pytest.raises(Exception, match="cancel_all_orders rejected"):
        asyncio.run(_mock_client(handler).cancel_all_orders(category="linear", settle_coin="USDT"))


def test_venue_errors_keep_their_code():
    import asyncio

    import httpx
    import pytest

    def handler(request):
        return httpx.Response(200, json={"retCode": 110007, "retMsg": "ab not enough for new order"})

    with pytest.raises(Exception, match="110007"):
        asyncio.run(_mock_client(handler).get_positions(symbol="BTCUSDT"))
