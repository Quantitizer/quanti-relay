"""Rate-limit resilience for the Bybit client — the fix for the patient maker-chase tripping the
circuit breaker. A full-book patient rebalance can fan out dozens of concurrent legs; a brief Bybit
throttle (retCode 10006/10018 / HTTP 429) must be retried with backoff, NOT raised into the circuit
breaker (5 failures opens it for 60s and cascades the rebalance into skipped legs + failed cancels).
"""
import asyncio

from relay.exchange.bybit.rest_client import BybitRestClient


def _make_client():
    c = BybitRestClient(api_key="TESTKEY", api_secret="TESTSECRET", testnet=True)
    c._max_concurrent_req = 3
    c._req_sem = None
    return c


def test_rate_limit_error_is_retried_then_succeeds():
    c = _make_client()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("Bybit API error: retCode 10006 too many visits")
        return "ok"

    assert asyncio.run(c._with_timestamp_retry(flaky)) == "ok"
    assert calls["n"] == 3            # failed twice (retried), succeeded on the 3rd


def test_persistent_rate_limit_eventually_raises():
    c = _make_client()
    calls = {"n": 0}

    async def always_429():
        calls["n"] += 1
        raise Exception("HTTP 429 Too Many Requests")

    try:
        asyncio.run(c._with_timestamp_retry(always_429))
        assert False, "should have raised after exhausting retries"
    except Exception as e:
        assert "429" in str(e)
    assert calls["n"] == 4            # attempts 0,1,2 retried; attempt 3 raises to the circuit breaker


def test_non_rate_limit_error_is_not_retried():
    c = _make_client()
    calls = {"n": 0}

    async def hard_fail():
        calls["n"] += 1
        raise Exception("order not exists or too late to cancel")

    try:
        asyncio.run(c._with_timestamp_retry(hard_fail))
        assert False
    except Exception:
        pass
    assert calls["n"] == 1            # a real error is NOT swallowed by the rate-limit retry


def test_is_rate_limit_error_classification():
    assert BybitRestClient._is_rate_limit_error(Exception("retCode 10006 too many visits"))
    assert BybitRestClient._is_rate_limit_error(Exception("retCode 10018 rate limit"))
    assert BybitRestClient._is_rate_limit_error(Exception("HTTP 429 Too Many Requests"))
    # A genuine failure must NOT be treated as a transient throttle.
    assert not BybitRestClient._is_rate_limit_error(Exception("order not exists"))
    assert not BybitRestClient._is_rate_limit_error(Exception("Error sign"))
    assert not BybitRestClient._is_rate_limit_error(Exception("Circuit breaker is OPEN"))
