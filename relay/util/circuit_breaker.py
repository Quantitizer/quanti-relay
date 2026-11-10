"""
Circuit breaker pattern implementation for connector fault tolerance.
"""
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from loguru import logger

from relay.config import settings


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


class BenignOrderStateError(Exception):
    """An exchange call that FAILED for a reason that is a normal business outcome, not a fault.

    The archetype: the maker-chase amends a resting post-only order at the exact moment it FILLS, so
    the amend returns "order not exists or too late to replace". The venue answered us correctly and
    promptly — the order simply left the book first. Callers already treat this as success (re-price,
    or 'already filled/gone'), and it must NOT count toward opening the breaker.

    THE BUG THIS EXISTS FOR: every exception counted as a failure, so five fills-during-amend in a row
    — a sign the chase is working WELL — opened the breaker for 60s and killed the rest of the
    rebalance. The better the fills, the more likely the trip - with no rate-limit codes from the
    exchange at all.

    Subclasses Exception so existing `except Exception` handlers and message-substring checks in the
    executor keep working unchanged.
    """


class CircuitBreaker:
    """
    Circuit breaker to prevent cascade failures.
    
    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, reject requests immediately
    - HALF_OPEN: After timeout, allow one request to test recovery
    
    The circuit opens after N consecutive failures and stays open
    for a timeout period before attempting recovery.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int | None = None,
        timeout: int | None = None,
        on_state_change: Callable[[str, str], None] | None = None,
    ):
        """
        Initialize circuit breaker.
        
        Args:
            name: Identifier for this circuit (e.g., connector name)
            failure_threshold: Number of failures before opening circuit
            timeout: Seconds to wait before attempting recovery
            on_state_change: Optional callback(old_state, new_state) when state changes
        """
        self.name = name
        self._on_state_change = on_state_change
        self.failure_threshold = (
            failure_threshold or settings.circuit_breaker_failure_threshold
        )
        self.timeout = timeout or settings.circuit_breaker_timeout

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.last_success_time: float | None = None

    def allows_requests(self) -> bool:
        """False only while OPEN and the cool-down has not elapsed. (The OPEN -> HALF_OPEN move
        itself happens on the next call; waiters use this to know a call is allowed again.)"""
        return self.state != CircuitState.OPEN or self._should_attempt_reset()

    async def call_async(
        self, func: Callable[..., Any], *args: Any, bypass_open: bool = False, **kwargs: Any
    ) -> Any:
        """
        Execute async function through circuit breaker.
        
        Args:
            func: Async function to execute
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func
        
        Returns:
            Result from func
        
        Raises:
            CircuitBreakerOpen: If circuit is open
            Exception: Any exception from func

        `bypass_open=True` is for risk-REDUCING requests (reduce-only closes, protective stops): the
        breaker exists to stop new risk while the venue is failing, never to stop the relay from
        flattening or protecting a position. The outcome still counts toward the breaker.
        """
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitState.HALF_OPEN
            elif not bypass_open:
                raise CircuitBreakerOpen(
                    f"Circuit breaker '{self.name}' is OPEN. "
                    f"Last failure: {self.last_failure_time}"
                )

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except BenignOrderStateError:
            raise   # a normal business outcome, not a fault (neither success nor failure)
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        """Handle successful call."""
        old_state = self.state.value
        self.failure_count = 0
        self.last_success_time = time.time()
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            if self._on_state_change:
                try:
                    self._on_state_change(old_state, self.state.value)
                except Exception as e:
                    logger.warning(f"[CircuitBreaker:{self.name}] state-change callback failed: {e}")

    def _on_failure(self) -> None:
        """Handle failed call."""
        old_state = self.state.value
        self.failure_count += 1
        self.last_failure_time = time.time()

        # A failed probe after the cool-down re-opens at once (the venue is still failing); otherwise
        # the threshold of consecutive failures applies.
        if self.state != CircuitState.OPEN and (
                self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold):
            self.state = CircuitState.OPEN
            if self._on_state_change:
                try:
                    self._on_state_change(old_state, self.state.value)
                except Exception as e:
                    logger.warning(f"[CircuitBreaker:{self.name}] state-change callback failed: {e}")

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        if self.last_failure_time is None:
            return True

        elapsed = time.time() - self.last_failure_time
        return elapsed >= self.timeout

    def reset(self) -> None:
        """Manually reset circuit breaker to CLOSED state."""
        old_state = self.state.value
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        if self._on_state_change:
            try:
                self._on_state_change(old_state, self.state.value)
            except Exception as e:
                logger.warning(f"[CircuitBreaker:{self.name}] state-change callback failed: {e}")

    def get_status(self) -> dict:
        """
        Get current circuit breaker status.
        
        Returns:
            Dictionary with status information
        """
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
        }


class CircuitBreakerOpen(Exception):
    """Exception raised when circuit breaker is open."""


