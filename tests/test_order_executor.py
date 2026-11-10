import asyncio

from relay.execution import order_executor as oe
from relay.execution.order_executor import OrderExecutor
from relay.util.circuit_breaker import CircuitBreakerOpen


class _FakeBybit:
    def __init__(self, place_results=(), history=None, last_price="101.5"):
        self.placed = []
        self.lookups = []
        self._results = list(place_results)
        self._history = history or []
        self._last = last_price

    async def place_market_order(self, **kw):
        self.placed.append(kw)
        r = self._results.pop(0) if self._results else {"orderId": "o1", "avgPrice": "100", "cumExecQty": "1"}
        if isinstance(r, Exception):
            raise r
        return r

    async def get_order_history(self, **kw):
        self.lookups.append(kw)
        h = self._history(kw) if callable(self._history) else self._history
        return h

    async def get_ticker(self, symbol, category="linear"):
        return {"lastPrice": self._last}


def _no_sleep(monkeypatch):
    async def _sleep(_):
        return None
    monkeypatch.setattr(oe.asyncio, "sleep", _sleep)


def test_dry_run_places_nothing():
    fake = _FakeBybit()
    ex = OrderExecutor(fake)  # dry_run defaults to True
    r = asyncio.run(ex.place_order("BTCUSDT", "long", 0.5, expected_price=100.0))
    assert r["success"] and r["order_id"].startswith("dry_run_")
    c = asyncio.run(ex.close_position("BTCUSDT", "long", 0.5))
    assert c["fill_price"] == 101.5   # dry-run close uses the market price, never 0
    assert fake.placed == []


_FILLED = [{"orderStatus": "Filled", "avgPrice": "100.2", "cumExecQty": "0.25", "orderId": "o1"}]


def test_live_entry_takes_price_and_size_from_the_venues_order_record():
    """The create response carries no fill: the recorded entry is the venue's average price."""
    fake = _FakeBybit([{"orderId": "o1"}], history=_FILLED)
    ex = OrderExecutor(fake, dry_run=False)
    r = asyncio.run(ex.place_order("BTCUSDT", "short", 0.25, expected_price=100.0, stop_loss_price=103.5))
    assert r["success"] and r["fill_price"] == 100.2 and r["fill_quantity"] == 0.25
    assert r["fill_source"] == "venue_order" and round(r["slippage_pct"], 6) == 0.2
    call = fake.placed[0]
    assert call["side"] == "Sell" and call["qty"] == "0.25" and call["stop_loss"] == "103.5"


def test_a_partial_fill_records_what_actually_filled(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([{"orderId": "o1"}], history=[
        {"orderStatus": "PartiallyFilledCanceled", "avgPrice": "100", "cumExecQty": "0.4", "orderId": "o1"}])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1))
    assert r["success"] and r["fill_quantity"] == 0.4


def test_an_unconfirmed_fill_is_reported_not_guessed(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([{"orderId": "o1", "avgPrice": "0"}], history=[])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1, expected_price=100.0))
    assert r["success"] and r["fill_price"] is None and r["fill_source"] == "unconfirmed"


def test_non_retryable_error_stops_immediately(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([Exception("Bybit API error: insufficient balance")] * 3)
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1))
    assert not r["success"] and len(fake.placed) == 1


def test_a_venue_rejection_is_retried(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([Exception("Bybit API error: Too many visits"), {"orderId": "o2"}],
                      history=[{"orderStatus": "Filled", "avgPrice": "99", "cumExecQty": "1", "orderId": "o2"}])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1))
    assert r["success"] and r["order_id"] == "o2" and len(fake.placed) == 2


def test_a_timeout_on_an_order_the_venue_accepted_is_never_resent(monkeypatch):
    """The request timed out after the venue took the order: re-sending would be a second order."""
    _no_sleep(monkeypatch)
    fake = _FakeBybit([Exception("ReadTimeout")], history=lambda kw: (
        [{"orderStatus": "Filled", "avgPrice": "99", "cumExecQty": "1", "orderId": "o1"}]
        if kw.get("order_link_id") == "rl-d1-abc" else []))
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1, order_link_id="rl-d1-abc"))
    assert r["success"] and r["fill_price"] == 99.0 and len(fake.placed) == 1


def test_a_tagged_order_the_venue_cannot_find_after_a_timeout_is_not_resent(monkeypatch):
    """Some venues accept the same tag again once the first order filled (unique among open orders
    only), and a lookup can lag: a re-send could double the position."""
    _no_sleep(monkeypatch)
    fake = _FakeBybit([Exception("ConnectError"), {"orderId": "o2"}], history=lambda kw: [])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1, order_link_id="rl-d1-abc"))
    assert not r["success"] and r["outcome_unknown"] and len(fake.placed) == 1


def test_a_clear_refusal_is_still_retried(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([Exception("Venue rejected: busy, try again"), {"orderId": "o2"}],
                      history=lambda kw: [{"orderStatus": "Filled", "avgPrice": "99", "cumExecQty": "1",
                                           "orderId": "o2"}])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1, order_link_id="rl-d1-abc"))
    assert r["success"] and len(fake.placed) == 2


def test_an_untagged_order_with_an_unknown_outcome_is_not_resent(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([Exception("ReadTimeout"), {"orderId": "o2"}])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1))
    assert not r["success"] and r["outcome_unknown"] and len(fake.placed) == 1


def test_open_circuit_fails_fast():
    fake = _FakeBybit([CircuitBreakerOpen("open")])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1))
    assert not r["success"] and r["circuit_breaker_open"] and len(fake.placed) == 1


def test_close_is_reduce_only_and_polls_fill_price(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([{"orderId": "c1", "avgPrice": ""}],
                      history=[{"orderStatus": "Filled", "avgPrice": "98.7"}])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).close_position("BTCUSDT", "long", 1))
    assert r["success"] and r["fill_price"] == 98.7
    assert fake.placed[0]["reduce_only"] is True and fake.placed[0]["side"] == "Sell"


def test_a_real_positions_close_is_real_whatever_the_flag_says():
    fake = _FakeBybit([{"orderId": "f1"}])
    r = asyncio.run(OrderExecutor(fake).close_position_fast("BTCUSDT", "short", 2, simulate=False))
    assert r["success"] and fake.placed[0]["reduce_only"] is True and fake.placed[0]["side"] == "Buy"


def test_a_paper_positions_close_is_simulated_whatever_the_flag_says():
    fake = _FakeBybit([{"orderId": "f1"}])
    ex = OrderExecutor(fake, dry_run=False)                    # the relay is live now ...
    asyncio.run(ex.close_position_fast("BTCUSDT", "short", 2, simulate=True))   # ... the position is paper
    asyncio.run(ex.close_position("BTCUSDT", "short", 2, simulate=True))
    assert fake.placed == []


def test_close_quantities_are_plain_decimals():
    from relay.execution.order_executor import format_order_qty
    assert format_order_qty(1e-05) == "0.00001" and format_order_qty(0.1 + 0.2) == "0.3"
    assert format_order_qty(3.0) == "3.0"


def test_dry_run_callable_is_read_per_call():
    state = {"dry": True}
    fake = _FakeBybit()
    ex = OrderExecutor(fake, dry_run=lambda: state["dry"])
    asyncio.run(ex.place_order("BTCUSDT", "long", 1))
    state["dry"] = False
    asyncio.run(ex.place_order("BTCUSDT", "long", 1))
    assert len(fake.placed) == 1


def test_an_order_that_ended_with_nothing_filled_is_a_failure(monkeypatch):
    _no_sleep(monkeypatch)
    fake = _FakeBybit([{"orderId": "o1"}], history=[
        {"orderStatus": "Cancelled", "avgPrice": "", "cumExecQty": "0", "orderId": "o1"}])
    r = asyncio.run(OrderExecutor(fake, dry_run=False).place_order("BTCUSDT", "long", 1))
    assert not r["success"] and "nothing filled" in r["error"]


def test_the_order_lookup_sends_the_relays_tag():
    import httpx

    from relay.exchange.bybit.rest_client import BybitRestClient
    seen = {}

    def handler(request):
        if request.url.path == "/v5/order/history":
            seen.update(dict(request.url.params))
        if request.url.path == "/v5/market/time":
            import time
            return httpx.Response(200, json={"retCode": 0, "result": {"timeNano": str(time.time_ns())}})
        return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})

    c = BybitRestClient(api_key="k", api_secret="s", demo=True)
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=c.base_url)
    asyncio.run(c.get_order_history(symbol="BTCUSDT", order_link_id="rl-d1-abc"))
    assert seen.get("orderLinkId") == "rl-d1-abc"
