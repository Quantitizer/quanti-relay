"""Regression tests for the Bybit cancel-signing bug (an 'Error sign' storm on every cancel).

`cancel_order` / `cancel_all_orders` must SIGN and SEND the identical compact+sorted JSON body.
Bybit signs the raw bytes it receives, so sending the body via httpx `json=` (which re-serializes
with spaces + insertion order) while signing the compact string makes EVERY cancel fail with
'Error sign' — which the SmartExecutor then mislabeled as "already filled/gone". These tests assert
the bytes sent are exactly the bytes signed.
"""
import asyncio
import hashlib
import hmac
import json

from relay.exchange.bybit.rest_client import BybitRestClient


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"retCode": 0, "result": {}}


class _CapturingHTTP:
    """Stands in for httpx.AsyncClient — records the last POST (url, content, json, headers)."""

    def __init__(self):
        self.calls = []

    async def post(self, url, content=None, json=None, headers=None):
        self.calls.append({"url": url, "content": content, "json": json, "headers": headers})
        return _FakeResponse()


def _make_client():
    c = BybitRestClient(api_key="test-key", api_secret="TESTSECRET", testnet=True)
    c.client = _CapturingHTTP()
    c._time_offset_ms = 0

    async def _no_sync():  # skip the /v5/market/time network call
        return None

    c._ensure_time_synced = _no_sync
    return c


def _expected_sig(client, ts, rw, sent_body: str) -> str:
    payload = f"{ts}{client.api_key}{rw}{sent_body}"
    return hmac.new(client.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def test_cancel_order_signs_exactly_the_sent_body():
    client = _make_client()
    asyncio.run(client.cancel_order(symbol="BTCUSDT", order_id="abc-123", category="linear"))

    call = client.client.calls[-1]
    assert call["url"] == "/v5/order/cancel"
    # Must be sent as raw content, NEVER httpx json= (which re-serializes with spaces -> mismatch).
    assert call["json"] is None
    sent = call["content"]
    assert sent is not None and " " not in sent
    assert sent == json.dumps(
        {"category": "linear", "symbol": "BTCUSDT", "orderId": "abc-123"},
        sort_keys=True, separators=(",", ":"),
    )
    # The signature must cover EXACTLY the bytes sent — this is what Bybit validates.
    h = call["headers"]
    ts, rw = int(h["X-BAPI-TIMESTAMP"]), h["X-BAPI-RECV-WINDOW"]
    assert h["X-BAPI-SIGN"] == _expected_sig(client, ts, rw, sent)
    # ...and it must DIFFER from the old query-string signature (the exact regression we fixed).
    buggy = client._generate_signature(
        {"category": "linear", "symbol": "BTCUSDT", "orderId": "abc-123"},
        ts, rw, use_json_body=False,
    )
    assert h["X-BAPI-SIGN"] != buggy


def test_cancel_all_orders_signs_exactly_the_sent_body():
    client = _make_client()
    asyncio.run(client.cancel_all_orders(symbol="ETHUSDT", category="linear"))

    call = client.client.calls[-1]
    assert call["url"] == "/v5/order/cancel-all"
    assert call["json"] is None
    sent = call["content"]
    assert sent is not None and " " not in sent
    h = call["headers"]
    ts, rw = int(h["X-BAPI-TIMESTAMP"]), h["X-BAPI-RECV-WINDOW"]
    assert h["X-BAPI-SIGN"] == _expected_sig(client, ts, rw, sent)



def test_trailing_stop_is_sent_as_a_price_distance_with_its_activation_and_no_stop_loss():
    client = _make_client()
    asyncio.run(client.set_trading_stop(symbol="BTCUSDT", trailing_stop=780.0, active_price=80339.8))
    call = client.client.calls[-1]
    body = json.loads(call["content"])
    assert call["url"] == "/v5/position/trading-stop"
    assert (body["trailingStop"], body["activePrice"], body["tpslMode"]) == ("780", "80339.8", "Full")
    assert "stopLoss" not in body and "takeProfit" not in body       # the fixed stop is left alone
    asyncio.run(client.set_trading_stop(symbol="BTCUSDT", trailing_stop=0))
    body = json.loads(client.client.calls[-1]["content"])
    assert body["trailingStop"] == "0" and "activePrice" not in body


def test_get_order_reads_one_order_in_real_time():
    class _Orders(_CapturingHTTP):
        async def get(self, url, params=None, headers=None):
            self.calls.append({"url": url, "params": dict(params), "headers": headers})
            resp = _FakeResponse()
            resp.json = lambda: {"retCode": 0, "result": {"list": [
                {"orderId": "other", "orderLinkId": "x", "orderStatus": "New"},
                {"orderId": "abc", "orderLinkId": "rl-1", "orderStatus": "Filled", "cumExecQty": "0.001"}]}}
            return resp
    client = _make_client()
    client.client = _Orders()
    o = asyncio.run(client.get_order(symbol="BTCUSDT", order_id="abc"))
    call = client.client.calls[-1]
    assert call["url"] == "/v5/order/realtime"          # not /v5/order/history: that lags
    assert call["params"] == {"category": "linear", "symbol": "BTCUSDT", "orderId": "abc"}
    assert o["orderStatus"] == "Filled"
    assert asyncio.run(client.get_order(symbol="BTCUSDT", order_link_id="rl-1"))["orderId"] == "abc"
    assert asyncio.run(client.get_order(symbol="BTCUSDT", order_id="missing")) is None
