"""A WORKING maker-chase must not be able to open the trading circuit breaker.

THE BUG: CircuitBreaker counted EVERY exception as a failure. During a
patient maker-chase the resting post-only order routinely FILLS while an amend is in flight, and
Bybit answers "order not exists or too late to replace". That is the chase succeeding — but it
counted as a fault, and five in a row opened the breaker for 60s, killing the remainder of the
rebalance. No rate-limit codes were present: the venue was healthy throughout. The better the fill
rate, the more likely the trip.

Guards both halves: the breaker ignores the benign type, and the Bybit client actually raises it.
"""
import asyncio

import pytest

from relay.exchange.bybit.rest_client import is_benign_order_state
from relay.util.circuit_breaker import (
    BenignOrderStateError,
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
)


def _breaker(threshold=5):
    return CircuitBreaker(name="test", failure_threshold=threshold, timeout=60)


def _raise(exc):
    async def _f():
        raise exc
    return _f


# --------------------------------------------------------------------------------------
# the breaker
# --------------------------------------------------------------------------------------

def test_benign_races_never_open_the_breaker():
    """THE FIX. Twenty fills-during-amend — four times the threshold — leave it CLOSED."""
    cb = _breaker()
    for _ in range(20):
        with pytest.raises(BenignOrderStateError):
            asyncio.run(cb.call_async(_raise(
                BenignOrderStateError("Bybit API error: order not exists or too late to replace"))))
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


def test_real_faults_still_open_the_breaker():
    """The safety mechanism must still work — this is the whole point of narrowing it carefully."""
    cb = _breaker(threshold=5)
    for _ in range(5):
        with pytest.raises(Exception):
            asyncio.run(cb.call_async(_raise(TimeoutError("read timeout"))))
    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpen):
        asyncio.run(cb.call_async(_raise(TimeoutError("read timeout"))))


def test_benign_does_not_reset_a_real_failure_streak():
    """Neither success nor failure. A genuine degradation interleaved with benign races must still
    reach the threshold — treating benign as success would mask a failing venue indefinitely."""
    cb = _breaker(threshold=3)
    for _ in range(2):
        with pytest.raises(Exception):
            asyncio.run(cb.call_async(_raise(TimeoutError("boom"))))
    assert cb.failure_count == 2

    with pytest.raises(BenignOrderStateError):
        asyncio.run(cb.call_async(_raise(BenignOrderStateError("too late to cancel"))))
    assert cb.failure_count == 2, "benign must not reset the streak"

    with pytest.raises(Exception):
        asyncio.run(cb.call_async(_raise(TimeoutError("boom"))))
    assert cb.state == CircuitState.OPEN


def test_a_real_success_still_resets():
    cb = _breaker(threshold=3)
    for _ in range(2):
        with pytest.raises(Exception):
            asyncio.run(cb.call_async(_raise(TimeoutError("boom"))))

    async def _ok():
        return "fine"
    assert asyncio.run(cb.call_async(_ok)) == "fine"
    assert cb.failure_count == 0


# --------------------------------------------------------------------------------------
# the classifier — deliberately narrow
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "Bybit API error: order not exists or too late to replace",
    "Bybit API error: order not exists or too late to cancel",
    "ORDER NOT EXISTS OR TOO LATE TO CANCEL",          # case-insensitive
])
def test_benign_messages_are_recognised(msg):
    assert is_benign_order_state(msg) is True


@pytest.mark.parametrize("msg", [
    "Bybit API error: Too many visits. Exceeded the API Rate Limit",   # 10006 — a REAL fault
    "Bybit API error: IP rate limit exceeded",                          # 10018 — a REAL fault
    "ReadTimeout: timed out",
    "Bybit API error: Insufficient balance",
    "Bybit API error: Error sign",                                      # auth — must never be benign
    "",
])
def test_everything_else_still_counts_as_a_failure(msg):
    """If this list ever shrinks, the breaker has been quietly disarmed."""
    assert is_benign_order_state(msg) is False


def test_none_is_safe():
    assert is_benign_order_state(None) is False
