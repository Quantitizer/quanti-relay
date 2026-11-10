"""NOT WIRED YET: the running relay does not use this module. It is kept for phase 2, if multi-account load needs it beyond the client's own limit, and reviewed and fixed when it is connected (tests/test_unwired_modules.py keeps the running relay from importing it before then).


Rate limiter for API calls using token bucket algorithm.

Provides proactive request throttling to stay under exchange rate limits,
unlike circuit breaker which reactively blocks after failures.
"""

import asyncio
import time


class RateLimiter:
    """
    Token bucket rate limiter for API calls.
    
    Targets 50 req/sec by default (safety margin under Bybit's 60/sec limit).
    Proactively throttles requests instead of blocking after rate limit errors.
    
    Usage:
        limiter = RateLimiter(requests_per_second=50.0)
        result = await limiter.call_async(some_async_function, arg1, arg2)
    """
    
    def __init__(self, requests_per_second: float = 50.0):
        """
        Initialize rate limiter.
        
        Args:
            requests_per_second: Maximum requests per second (default: 50.0)
        """
        self.requests_per_second = requests_per_second
        self.min_interval = 1.0 / requests_per_second  # Time between requests
        self.last_request_time = 0.0
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        """
        Wait until we can make a request without exceeding rate limit.
        
        Thread-safe via asyncio.Lock to handle concurrent callers.
        """
        async with self._lock:
            now = time.monotonic()
            time_since_last = now - self.last_request_time
            
            if time_since_last < self.min_interval:
                wait_time = self.min_interval - time_since_last
                await asyncio.sleep(wait_time)
            
            self.last_request_time = time.monotonic()
    
    async def call_async(self, func, *args, **kwargs):
        """
        Execute async function with rate limiting.
        
        Args:
            func: Async function to execute
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func
        
        Returns:
            Result from func
        """
        await self.acquire()
        return await func(*args, **kwargs)
    
    def get_status(self) -> dict:
        """
        Get current rate limiter status.
        
        Returns:
            Dictionary with status information
        """
        return {
            "requests_per_second": self.requests_per_second,
            "min_interval_ms": self.min_interval * 1000,
            "last_request_time": self.last_request_time,
        }




