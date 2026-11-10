"""
In-process failure counters for exchange calls.

Incremented by the exchange clients and their circuit breakers; exposed read-only through the
API's metrics endpoint.
"""

# Bybit failure counters (incremented by the Bybit REST client and its circuit breaker)
_bybit_failures: dict[str, int] = {
    "timeout": 0,
    "5xx": 0,
    "circuit_breaker_open": 0,
}


def increment_bybit_failure(kind: str) -> None:
    """Increment a Bybit failure counter. kind: timeout, 5xx, circuit_breaker_open."""
    if kind in _bybit_failures:
        _bybit_failures[kind] += 1


def get_bybit_failures() -> dict[str, int]:
    """Return current Bybit failure counts (copy)."""
    return dict(_bybit_failures)
