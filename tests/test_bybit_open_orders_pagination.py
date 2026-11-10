"""get_open_orders pagination.

WHY THIS EXISTS: the orphan-order sweep is the ONLY thing that clears resting post-only children
left by a crashed or restarted process — Bybit PostOnly/GTC orders have no server-side expiry. It
enumerates via `get_open_orders`, which once sent no `limit` and followed no cursor, so it saw only
Bybit's default first page of 20. A patient book rebalance can rest dozens of children
concurrently, so the sweep silently cancelled at most 20 of them and reported success.
"""
import asyncio

from relay.exchange.bybit.rest_client import BybitRestClient


def _mk(pages):
    """A client whose GET returns `pages` in order. Records the params of every request."""
    client = BybitRestClient.__new__(BybitRestClient)          # no network, no credentials
    sent = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _HTTP:
        async def get(self, path, params=None, headers=None):
            sent.append(dict(params or {}))
            idx = min(len(sent) - 1, len(pages) - 1)
            return _Resp(pages[idx])

    class _CB:
        async def call_async(self, fn):
            return await fn()

    async def _noop():
        return None

    client.client = _HTTP()
    client.circuit_breaker = _CB()
    client._ensure_time_synced = _noop
    client._get_auth_headers = lambda p: {}
    client._with_timestamp_retry = lambda fn: fn()
    client._log_bybit_error = lambda *a, **k: _noop()
    return client, sent


def _page(rows, cursor=None):
    return {"retCode": 0, "result": {"list": rows, "nextPageCursor": cursor}}


def test_get_open_orders_paginates_past_the_first_page():
    """The whole point: a book bigger than one page must come back whole."""
    p1 = _page([{"orderId": f"o{i}"} for i in range(50)], cursor="CUR")
    p2 = _page([{"orderId": f"o{i}"} for i in range(50, 54)], cursor=None)
    client, sent = _mk([p1, p2])
    out = asyncio.run(client.get_open_orders(category="linear", settle_coin="USDT"))
    assert len(out) == 54, "the sweep must see the whole resting book, not just page 1"
    assert len(sent) == 2 and sent[1].get("cursor") == "CUR"


def test_get_open_orders_sends_limit_clamped_to_50():
    """/v5/order/realtime caps `limit` at 50 — copying get_positions' 200 would be rejected."""
    client, sent = _mk([_page([], cursor=None)])
    asyncio.run(client.get_open_orders(category="linear", settle_coin="USDT", limit=200))
    assert sent[0]["limit"] == 50


def test_get_open_orders_stops_on_an_empty_page():
    """A cursor that never clears must not spin — bounded like get_positions."""
    client, sent = _mk([_page([], cursor="NEVER_ENDS")])
    out = asyncio.run(client.get_open_orders(category="linear"))
    assert out == [] and len(sent) == 1


def test_get_open_orders_retry_restarts_from_page_one():
    """THE PARAMS-COPY BUG. `_with_timestamp_retry` re-runs the whole request body. If the params
    dict were shared across attempts, a `cursor` set before a mid-pagination failure would survive
    into the retry — which would then resume from page 2 and silently drop page 1's orders. Each
    attempt must start clean."""
    p1 = _page([{"orderId": "a"}], cursor="CUR")
    client, sent = _mk([p1, _page([{"orderId": "b"}], cursor=None)])

    async def _retry(fn):
        await fn()                          # attempt 1: walks to page 2, then "fails" on timestamp drift
        return await fn()                   # attempt 2: the retry _with_timestamp_retry performs

    client._with_timestamp_retry = _retry
    sent.clear()
    asyncio.run(client.get_open_orders(category="linear", settle_coin="USDT"))
    first_of_retry = sent[2]                # attempt 1 issued 2 requests; attempt 2 starts at index 2
    assert "cursor" not in first_of_retry, \
        "a leaked cursor would make the retry resume mid-book and drop page 1"
