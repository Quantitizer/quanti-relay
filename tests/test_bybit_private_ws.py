import asyncio
import hashlib
import hmac
import json
import time

import websockets

from relay.exchange.bybit.private_ws import (
    BybitPrivateWsClient,
    OrderStatus,
    OrderUpdate,
    PositionSide,
    PositionUpdate,
)


def test_websockets_is_the_legacy_client():
    """The heartbeat loop checks `ws.open`, which only the legacy client (websockets < 14) has.
    On the new client that attribute is gone, the heartbeat errors every 20s and the connection
    is torn down and rebuilt forever. If this fails, port the heartbeat before upgrading."""
    assert int(websockets.__version__.split(".")[0]) < 14
    assert "legacy" in websockets.connect.__module__


def test_position_update_parsing():
    p = PositionUpdate.from_bybit_data({
        "symbol": "BTCUSDT", "side": "Sell", "size": "0.5", "entryPrice": "60000",
        "markPrice": "59000", "unrealisedPnl": "500", "cumRealisedPnl": "-3", "leverage": "2",
        "liqPrice": "", "takeProfit": "", "stopLoss": "61500", "updatedTime": "1700000000000",
    })
    assert p.side is PositionSide.SHORT
    assert p.size == 0.5
    assert p.liq_price is None                       # empty = unknown, not 0
    assert p.take_profit is None
    assert p.stop_loss == 61500.0


def test_unknown_enums_fall_back():
    assert PositionUpdate.from_bybit_data({"side": "weird"}).side is PositionSide.NONE
    assert OrderUpdate.from_bybit_data({"orderStatus": "Untriggered"}).status is OrderStatus.UNTRIGGERED


def test_order_update_parsing():
    o = OrderUpdate.from_bybit_data({
        "orderId": "1", "orderLinkId": "rl-1", "symbol": "ETHUSDT", "side": "Buy",
        "orderType": "Limit", "price": "3000", "qty": "1", "cumExecQty": "0.4",
        "avgPrice": "2999.5", "orderStatus": "PartiallyFilled", "reduceOnly": False,
    })
    assert o.status is OrderStatus.PARTIALLY_FILLED
    assert o.filled_qty == 0.4
    assert o.order_link_id == "rl-1"


def test_messages_dispatch_to_handlers_and_pong_is_tracked():
    c = BybitPrivateWsClient(api_key="k", api_secret="s", testnet=True)
    got = {"pos": [], "ord": [], "wal": []}

    async def on_pos(p):
        got["pos"].append(p)

    async def on_ord(o):
        got["ord"].append(o)

    async def on_wal(w):
        got["wal"].append(w)

    c.register_position_handler(on_pos)
    c.register_order_handler(on_ord)
    c.register_wallet_handler(on_wal)

    async def run():
        await c._handle_message(json.dumps({"topic": "position", "data": [{"symbol": "BTCUSDT", "side": "Buy", "size": "1"}]}))
        await c._handle_message(json.dumps({"topic": "order", "data": [{"symbol": "BTCUSDT", "orderStatus": "Filled"}]}))
        await c._handle_message(json.dumps({"topic": "wallet", "data": [{"coin": [{"coin": "USDT", "equity": "1000"}]}]}))
        await c._handle_message(json.dumps({"ret_msg": "pong"}))
        await c._handle_message("not json")  # must not raise

    asyncio.run(run())
    assert [p.symbol for p in got["pos"]] == ["BTCUSDT"]
    assert got["ord"][0].status is OrderStatus.FILLED
    assert got["wal"][0].equity == 1000.0
    assert c.get_position("BTCUSDT").size == 1.0
    assert 0 <= c.get_pong_age() < 5


def test_a_failing_handler_does_not_block_the_others():
    c = BybitPrivateWsClient(api_key="k", api_secret="s", testnet=True)
    seen = []

    async def bad(_):
        raise RuntimeError("boom")

    async def good(p):
        seen.append(p.symbol)

    c.register_position_handler(bad)
    c.register_position_handler(good)
    asyncio.run(c._handle_message(json.dumps({"topic": "position", "data": [{"symbol": "X"}]})))
    assert seen == ["X"]


def test_auth_message_signature():
    c = BybitPrivateWsClient(api_key="k", api_secret="s", testnet=True)
    sent = []

    class _Conn:
        async def send(self, m):
            sent.append(json.loads(m))

        async def recv(self):
            return json.dumps({"success": True})

    c.connection = _Conn()
    assert asyncio.run(c._authenticate()) is True
    op, (key, expires, sig) = sent[0]["op"], sent[0]["args"]
    assert op == "auth" and key == "k"
    assert sig == hmac.new(b"s", f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()


def test_health():
    c = BybitPrivateWsClient(api_key="k", api_secret="s", testnet=True)
    assert not c.is_healthy()  # not running
    c.is_running, c.connection = True, object()
    c._last_pong_ts = time.time()
    assert not c.is_healthy()  # connected but not logged in (e.g. the key was rejected)
    c.is_authenticated = True
    assert not c.is_healthy()                         # logged in but not subscribed: deaf
    c.is_subscribed = True
    assert c.is_healthy()
    c._last_pong_ts = time.time() - 120
    assert not c.is_healthy()


def test_endpoints():
    assert "stream-testnet" in BybitPrivateWsClient("k", "s", testnet=True).ws_url
    assert "stream-demo" in BybitPrivateWsClient("k", "s", testnet=False, demo=True).ws_url
    # demo wins over testnet, as in the REST client (testnet defaults on)
    assert "stream-demo" in BybitPrivateWsClient("k", "s", testnet=True, demo=True).ws_url
    assert BybitPrivateWsClient("k", "s", testnet=False, demo=False).ws_url == "wss://stream.bybit.com/v5/private"


def test_rejected_key_backs_off(monkeypatch):
    """A connection that opens but fails login must not reset the back-off (5s, 10s, 20s, ... 60s)."""
    import relay.exchange.bybit.private_ws as pw

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    c = BybitPrivateWsClient("k", "s", testnet=True)
    delays = []

    async def _auth():
        return False

    async def _sleep(d):
        delays.append(d)
        if len(delays) >= 4:
            c.is_running = False

    monkeypatch.setattr(pw.websockets, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(pw.asyncio, "sleep", _sleep)
    monkeypatch.setattr(c, "_authenticate", _auth)
    c.is_running = True
    asyncio.run(c._maintain_connection())
    assert delays == [5, 10, 20, 40]


def test_bybit_heartbeat_replies_count_as_pongs():
    """Bybit's real replies carry "op"; they must not be swallowed as operation responses."""
    c = BybitPrivateWsClient("k", "s", testnet=True)
    for reply in ({"req_id": "1", "op": "pong", "args": ["1700000000000"], "conn_id": "x"},  # private
                  {"success": True, "ret_msg": "pong", "conn_id": "x", "req_id": "", "op": "ping"}):  # public
        c._last_pong_ts = time.time() - 600  # stale, not 0 (0 means 'never' and reads as age 0)
        asyncio.run(c._handle_message(json.dumps(reply)))
        assert 0 <= c.get_pong_age() < 5, reply


def test_absent_position_fields_are_unknown_not_zero():
    """A message without 'size' must not read as size 0 (= closed) and close the row."""
    p = PositionUpdate.from_bybit_data({"symbol": "BTCUSDT", "side": "Buy", "markPrice": "100"})
    assert p.size is None and p.leverage is None and p.unrealized_pnl is None and p.mark_price == 100.0
    closed = PositionUpdate.from_bybit_data({"symbol": "BTCUSDT", "side": "", "size": "0"})
    assert closed.size == 0.0


def test_ec_noerror_is_not_a_rejection_and_unknown_statuses_stay_unknown():
    o = OrderUpdate.from_bybit_data({"orderStatus": "Filled", "rejectReason": "EC_NoError"})
    assert o.reject_reason is None
    u = OrderUpdate.from_bybit_data({"orderStatus": "Untriggered"})
    assert u.status is OrderStatus.UNTRIGGERED
    odd = OrderUpdate.from_bybit_data({"orderStatus": "SomethingNew"})
    assert odd.status is OrderStatus.UNKNOWN and odd.raw_status == "SomethingNew"


def test_a_failed_subscription_drops_the_connection_for_a_clean_resubscribe():
    c = BybitPrivateWsClient("k", "s", testnet=True)
    closed = []

    class _Conn:
        async def close(self):
            closed.append(True)

    c.connection, c.is_subscribed = _Conn(), True
    asyncio.run(c._handle_message(json.dumps({"op": "subscribe", "success": False, "ret_msg": "denied"})))
    assert closed and not c.is_subscribed
    asyncio.run(c._handle_message(json.dumps({"op": "subscribe", "success": True})))
    assert c.is_subscribed


def test_stop_ends_the_reconnect_loop():
    async def run():
        c = BybitPrivateWsClient("k", "s", testnet=True)

        async def _forever():
            await asyncio.sleep(3600)

        c._maintain_connection = _forever
        await c.start()
        task = c._task
        await c.stop()
        return task.done(), c._task              # checked before the event loop's own cleanup

    done, remaining = asyncio.run(run())
    assert done and remaining is None


def test_a_refused_subscription_backs_off_instead_of_reconnecting_in_a_loop(monkeypatch):
    """Login works, the subscription is refused, the socket closes normally: every reconnect must
    wait longer (5s, 10s, 20s ...), never hammer the venue."""
    import relay.exchange.bybit.private_ws as pw

    class _Conn:
        open = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def gen():
                yield json.dumps({"op": "subscribe", "success": False, "ret_msg": "permission denied"})
            return gen()

        async def close(self):
            self.open = False

        async def send(self, _):
            pass

    c = BybitPrivateWsClient("k", "s", testnet=True)
    delays = []

    async def _auth():
        c.is_authenticated = True
        return True

    async def _no_heartbeat(ws):
        return None

    async def _sleep(d):
        delays.append(d)
        if len(delays) >= 3:
            c.is_running = False

    monkeypatch.setattr(pw.websockets, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(pw.asyncio, "sleep", _sleep)
    monkeypatch.setattr(c, "_authenticate", _auth)
    monkeypatch.setattr(c, "_heartbeat_loop", _no_heartbeat)
    c.is_running = True
    asyncio.run(c._maintain_connection())
    assert delays == [5, 10, 20]
