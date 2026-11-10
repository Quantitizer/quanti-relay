import asyncio

import pytest

from relay.util.circuit_breaker import (
    BenignOrderStateError,
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
)


async def _boom():
    raise RuntimeError("venue down")


async def _benign():
    raise BenignOrderStateError("order not exists or too late to replace")


async def _ok():
    return "ok"


def test_opens_after_threshold():
    cb = CircuitBreaker("t", failure_threshold=3, timeout=60)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(cb.call_async(_boom))
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpen):
        asyncio.run(cb.call_async(_ok))


def test_benign_errors_never_open_the_breaker():
    cb = CircuitBreaker("t", failure_threshold=3, timeout=60)
    for _ in range(10):
        with pytest.raises(BenignOrderStateError):
            asyncio.run(cb.call_async(_benign))
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 0


def test_benign_errors_do_not_reset_real_failures():
    cb = CircuitBreaker("t", failure_threshold=3, timeout=60)
    for fn in (_boom, _benign, _boom, _benign, _boom):
        with pytest.raises(Exception):
            asyncio.run(cb.call_async(fn))
    assert cb.state is CircuitState.OPEN


def test_half_open_recovers_on_success():
    cb = CircuitBreaker("t", failure_threshold=1, timeout=1)
    with pytest.raises(RuntimeError):
        asyncio.run(cb.call_async(_boom))
    cb.last_failure_time -= 2  # pretend the timeout elapsed
    assert asyncio.run(cb.call_async(_ok)) == "ok"
    assert cb.state is CircuitState.CLOSED


def _tripped(timeout=60):
    cb = CircuitBreaker("t", failure_threshold=1, timeout=timeout)
    with pytest.raises(RuntimeError):
        asyncio.run(cb.call_async(_boom))
    assert cb.state is CircuitState.OPEN
    return cb


def test_risk_reducing_calls_pass_an_open_breaker():
    """A close or a protective stop must never be refused because the venue was failing."""
    cb = _tripped()
    with pytest.raises(CircuitBreakerOpen):
        asyncio.run(cb.call_async(_ok))
    assert asyncio.run(cb.call_async(_ok, bypass_open=True)) == "ok"


def test_allows_requests_once_the_cool_down_has_elapsed():
    cb = _tripped(timeout=60)
    assert not cb.allows_requests()
    cb.last_failure_time -= 61
    assert cb.allows_requests() and cb.state is CircuitState.OPEN   # moves to HALF_OPEN on the next call


def test_waiting_for_recovery_ends_when_the_cool_down_ends(monkeypatch):
    """close-all waits for recovery; before the fix the wait could only time out."""
    from relay.exchange.bybit.rest_client import BybitRestClient
    c = BybitRestClient(api_key="k", api_secret="s", demo=True)
    c.circuit_breaker.state = CircuitState.OPEN
    c.circuit_breaker.last_failure_time = __import__("time").time() - c.circuit_breaker.timeout + 0.2
    assert c.is_circuit_open()
    assert asyncio.run(c.wait_for_circuit_recovery(max_wait=5)) is True
    assert not c.is_circuit_open()


def test_bybit_closes_and_stops_bypass_an_open_breaker_but_entries_do_not(monkeypatch):
    from relay.exchange.bybit.rest_client import BybitRestClient
    c = BybitRestClient(api_key="k", api_secret="s", demo=True)

    async def _sent(coro_fn, bypass_throttle=False):
        return {"orderId": "o1", "retCode": 0}

    monkeypatch.setattr(c, "_with_timestamp_retry", _sent)
    c.circuit_breaker.state = CircuitState.OPEN
    c.circuit_breaker.last_failure_time = __import__("time").time()
    assert asyncio.run(c.place_market_order("BTCUSDT", "Sell", "0.001", reduce_only=True))["orderId"] == "o1"
    asyncio.run(c.set_trading_stop("BTCUSDT", stop_loss=95.0))
    with pytest.raises(CircuitBreakerOpen):
        asyncio.run(c.place_market_order("BTCUSDT", "Buy", "0.001"))


def test_a_failed_probe_after_the_cool_down_reopens_at_once():
    """Even after a successful risk-reducing call reset the failure count, a failing venue must not
    get a fresh run of failures to let new orders through."""
    cb = CircuitBreaker("t", failure_threshold=3, timeout=60)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(cb.call_async(_boom))
    assert asyncio.run(cb.call_async(_ok, bypass_open=True)) == "ok"   # a close got through
    cb.last_failure_time -= 61                                         # cool-down over
    with pytest.raises(RuntimeError):
        asyncio.run(cb.call_async(_boom))                              # the probe fails
    assert cb.state is CircuitState.OPEN


def test_failures_while_already_open_do_not_report_new_openings():
    opened = []
    cb = CircuitBreaker("t", failure_threshold=1, timeout=60,
                        on_state_change=lambda old, new: opened.append(new) if new == "open" else None)
    with pytest.raises(RuntimeError):
        asyncio.run(cb.call_async(_boom))
    for _ in range(3):                                                 # risk-reducing calls keep failing
        with pytest.raises(RuntimeError):
            asyncio.run(cb.call_async(_boom, bypass_open=True))
    assert opened == ["open"]


def test_safety_reads_pass_an_open_breaker_only_when_asked(monkeypatch):
    from relay.exchange.bybit.rest_client import BybitRestClient
    c = BybitRestClient(api_key="k", api_secret="s", demo=True)

    async def _sent(coro_fn, bypass_throttle=False):
        return 1234.0

    monkeypatch.setattr(c, "_with_timestamp_retry", _sent)
    c.circuit_breaker.state = CircuitState.OPEN
    c.circuit_breaker.last_failure_time = __import__("time").time()
    assert asyncio.run(c.get_account_balance(bypass_breaker=True)) == 1234.0
    with pytest.raises(CircuitBreakerOpen):
        asyncio.run(c.get_account_balance())
