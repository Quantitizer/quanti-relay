"""
Bybit REST API client for trading operations.

Direct implementation of Bybit v5 API (no CCXT) for order placement and position management.
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from typing import Any

import httpx
from loguru import logger

from relay.config import settings
from relay.util.circuit_breaker import BenignOrderStateError, CircuitBreaker

# Bybit retMsgs that mean "the order already left the book" — it filled, or a prior cancel won the
# race. During a maker-chase these are the EXPECTED outcome of a good fill, not a venue fault, so they
# are raised as BenignOrderStateError and never counted toward opening the circuit breaker.
# Deliberately narrow: matched on amend/cancel only. Timeouts, HTTP errors, auth failures, rate limits
# (10006/10018/429) and every other retCode still count as real failures and still open the breaker.
_BENIGN_ORDER_STATE_MARKERS = ("not exists or too late",)


def is_benign_order_state(msg: str) -> bool:
    """True when an amend/cancel failed only because the order had already left the book."""
    low = (msg or "").lower()
    return any(m in low for m in _BENIGN_ORDER_STATE_MARKERS)



def _num_str(value) -> str:
    """A price/quantity as a plain decimal string. str(float) can give '1.234e-05' or
    '0.30000000000000004'; Bybit rejects both."""
    if isinstance(value, str):
        return value
    from decimal import Decimal
    text = format(Decimal(repr(round(float(value), 10))), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _count_failure(e: Exception) -> None:
    """Feed the /metrics failure counters (timeouts and venue 5xx answers)."""
    from relay.monitoring.metrics import increment_bybit_failure
    if isinstance(e, httpx.TimeoutException):
        increment_bybit_failure("timeout")
    elif isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
        increment_bybit_failure("5xx")

class BybitRestClient:
    """
    Direct Bybit v5 REST API client for trading operations.
    
    Features:
    - Authenticated requests (API key + secret + signature)
    - Order placement (market orders)
    - Position management
    - Instrument listing
    - Automatic server time synchronization
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
        demo: bool | None = None,
    ):
        """
        Initialize Bybit REST client.
        
        Args:
            api_key: Bybit API key (defaults to env BYBIT_API_KEY)
            api_secret: Bybit API secret (defaults to env BYBIT_API_SECRET)
            testnet: Use testnet (defaults to settings.testnet)
            demo: Use demo trading mode (defaults to settings.bybit_demo)
        """
        self.api_key = api_key or os.environ.get("BYBIT_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("BYBIT_API_SECRET", "")
        self.testnet = testnet if testnet is not None else settings.testnet
        self.demo = demo if demo is not None else getattr(settings, 'bybit_demo', False)
        
        # Base URLs - Demo takes priority over testnet; regional endpoints mainnet only
        if self.demo:
            self.base_url = "https://api-demo.bybit.com"
        elif self.testnet:
            self.base_url = "https://api-testnet.bybit.com"
        else:
            region = getattr(settings, "bybit_region", "") or ""
            if str(region).lower() == "hk":
                self.base_url = "https://api.byhkbit.com"
            else:
                self.base_url = "https://api.bybit.com"
        
        # Use configurable timeout settings with defaults
        api_timeout = getattr(settings, 'api_timeout', 30.0)
        api_connect_timeout = getattr(settings, 'api_connect_timeout', 10.0)
        
        # HTTP client with timeout configuration
        # httpx.Timeout(total, connect) - total timeout and connection timeout
        timeout_config = httpx.Timeout(
            timeout=api_timeout,
            connect=api_connect_timeout
        )
        
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_config,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "quanti-relay python-httpx",
            }
        )
        
        # Time synchronization: offset between local time and Bybit server time
        # Positive offset means local time is behind server time
        self._time_offset_ms: int = 0
        self._recv_window: str = str(getattr(settings, 'bybit_recv_window_ms', 5000))
        self._last_sync_time: float = 0
        self._sync_interval: float = 60  # Re-sync every 1 minute
        # Bounds simultaneous in-flight requests so the patient maker-chase's concurrent fan-out can't
        # burst past Bybit's limits. Created lazily on first use so it binds to the running event loop.
        self._max_concurrent_req: int = int(getattr(settings, 'bybit_max_concurrent_requests', 6))
        self._req_sem: asyncio.Semaphore | None = None
        
        # Circuit breaker for API resilience (thresholds from settings: circuit_breaker_*)
        def _cb_state_change(old_state: str, new_state: str) -> None:
            (logger.warning if new_state == "open" else logger.info)(
                f"[BybitClient] circuit breaker {old_state} -> {new_state}")
            if new_state == "open":
                from relay.monitoring.metrics import increment_bybit_failure
                increment_bybit_failure("circuit_breaker_open")

        self.circuit_breaker = CircuitBreaker(
            name="bybit_trading",
            failure_threshold=settings.circuit_breaker_failure_threshold,
            timeout=settings.circuit_breaker_timeout,
            on_state_change=_cb_state_change,
        )
    
    def get_circuit_breaker_status(self) -> dict:
        """
        Get current circuit breaker status.
        
        Returns:
            Dict with:
                - state: 'closed', 'open', or 'half_open'
                - is_open: bool - True if circuit is blocking requests
                - failure_count: int - current failure count
                - last_failure_time: float | None - timestamp of last failure
                - seconds_until_recovery: float | None - seconds until circuit may recover (if open)
        """
        status = self.circuit_breaker.get_status()
        
        # Calculate seconds until recovery if circuit is open
        seconds_until_recovery = None
        if status['state'] == 'open' and status['last_failure_time']:
            elapsed = time.time() - status['last_failure_time']
            remaining = self.circuit_breaker.timeout - elapsed
            seconds_until_recovery = max(0, remaining)
        
        return {
            'state': status['state'],
            'is_open': status['state'] == 'open',
            'failure_count': status['failure_count'],
            'last_failure_time': status['last_failure_time'],
            'seconds_until_recovery': seconds_until_recovery,
        }
    
    async def _log_bybit_error(self, message: str, context: dict | None = None, level: str = "ERROR") -> None:
        """Log a Bybit API error with its context (e.g. {"endpoint": "place_order", "symbol": ...}).
        Timestamp-drift errors are logged as warnings: _with_timestamp_retry re-syncs and retries."""
        msg_lower = message.lower()
        if "timestamp" in msg_lower or "recv_window" in msg_lower:
            level = "WARNING"
            message = f"{message} (clock drift: re-synced and retried)"
        logger.log(level if level in ("ERROR", "WARNING") else "ERROR",
                   f"[BybitClient] {message} {context or ''}")

    def is_circuit_open(self) -> bool:
        """
        Check if the circuit breaker is currently open (blocking requests).
        
        Returns:
            True if circuit is open, False if closed or half-open
        """
        return not self.circuit_breaker.allows_requests()
    
    async def wait_for_circuit_recovery(self, max_wait: float = 60.0) -> bool:
        """
        Wait for circuit breaker to recover (transition out of OPEN state).
        
        Args:
            max_wait: Maximum seconds to wait for recovery
            
        Returns:
            True if circuit recovered, False if max_wait exceeded
        """
        import asyncio

        start_time = time.time()

        # allows_requests() turns True once the cool-down has elapsed; the breaker only moves to
        # HALF_OPEN on the next call, so waiting on `state` alone would never end.
        while not self.circuit_breaker.allows_requests():
            elapsed = time.time() - start_time
            if elapsed >= max_wait:
                return False
            
            # Check how long until recovery
            status = self.get_circuit_breaker_status()
            wait_time = status.get('seconds_until_recovery', 1.0)
            
            if wait_time and wait_time > 0:
                # Wait for the remaining time (plus small buffer)
                actual_wait = min(wait_time + 0.5, max_wait - elapsed)
                if actual_wait > 0:
                    await asyncio.sleep(actual_wait)
            else:
                # Small wait if we can't determine recovery time
                await asyncio.sleep(1.0)
        
        return True
    
    async def get_server_time(self) -> int:
        """
        Fetch current server time from Bybit.
        
        Uses the public /v5/market/time endpoint which requires no authentication.
        
        Returns:
            Server timestamp in milliseconds
            
        Raises:
            Exception: If API call fails
        """
        try:
            response = await self.client.get("/v5/market/time")
            response.raise_for_status()
            
            data = response.json()
            if data.get("retCode") != 0:
                raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
            
            # Response format: {"retCode": 0, "retMsg": "OK", "result": {"timeSecond": "1234567890", "timeNano": "1234567890123456789"}, "time": 1234567890123}
            # Use the 'time' field which is already in milliseconds
            server_time = data.get("time")
            if server_time:
                return int(server_time)
            
            # Fallback to timeSecond if 'time' not available
            result = data.get("result", {})
            time_second = result.get("timeSecond")
            if time_second:
                return int(time_second) * 1000
            
            raise Exception("No server time in response")
            
        except httpx.HTTPStatusError as e:
            raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise Exception(f"Bybit server time error: {e!s}")
    
    async def sync_time(self) -> int:
        """
        Synchronize local time with Bybit server time.
        
        Takes multiple samples and uses the lowest-latency measurement
        (NTP-style) to avoid poisoning the offset with a single slow
        or asymmetric HTTP round-trip.
        
        Returns:
            The calculated time offset in milliseconds (positive = local behind server)
        """
        _NUM_SAMPLES = 3
        _MAX_RTT_MS = 2000

        best_offset: int | None = None
        best_rtt = float('inf')

        for i in range(_NUM_SAMPLES):
            try:
                local_before = int(time.time() * 1000)
                server_time = await self.get_server_time()
                local_after = int(time.time() * 1000)

                rtt = local_after - local_before

                if rtt > _MAX_RTT_MS:
                    logger.info(f"[BybitClient] Time sync sample {i+1}/{_NUM_SAMPLES} "
                          f"discarded: RTT {rtt}ms exceeds {_MAX_RTT_MS}ms threshold")
                    continue

                latency = rtt // 2
                local_estimate = local_before + latency
                offset = server_time - local_estimate

                if rtt < best_rtt:
                    best_rtt = rtt
                    best_offset = offset

            except Exception as e:
                logger.warning(f"[BybitClient] Time sync sample {i+1}/{_NUM_SAMPLES} failed: {e}")
                continue

        if best_offset is not None:
            self._time_offset_ms = best_offset
            self._last_sync_time = time.time()

            if abs(self._time_offset_ms) > 1000:
                logger.info(f"[BybitClient] Time offset detected: {self._time_offset_ms}ms "
                      f"(local {'behind' if self._time_offset_ms > 0 else 'ahead of'} server) "
                      f"[best of {_NUM_SAMPLES} samples, RTT={best_rtt}ms]")
        else:
            logger.warning(f"[BybitClient] Time sync failed: all {_NUM_SAMPLES} samples "
                  f"failed or exceeded {_MAX_RTT_MS}ms RTT, keeping existing offset={self._time_offset_ms}ms")

        return self._time_offset_ms
    
    async def _ensure_time_synced(self) -> None:
        """
        Ensure time is synchronized, re-syncing if needed.
        
        Called before authenticated requests to maintain accurate timestamps.
        """
        current_time = time.time()
        
        # Sync if never synced or if sync interval has passed
        if self._last_sync_time == 0 or (current_time - self._last_sync_time) > self._sync_interval:
            await self.sync_time()
    
    @staticmethod
    def _is_timestamp_error(exc: Exception) -> bool:
        """Check if exception is a Bybit timestamp/recv_window validation error."""
        msg = str(exc).lower()
        if "timestamp" in msg or "recv_window" in msg:
            return True
        if hasattr(exc, "response") and exc.response is not None:
            body = getattr(exc.response, "text", "") or ""
            return "timestamp" in body.lower() or "recv_window" in body.lower()
        return False

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Bybit throttle / rate limit: retCode 10006 (too many visits) / 10018 (rate limit) / HTTP 429.
        These are TRANSIENT — under the patient maker-chase's concurrent fan-out a brief burst should
        back off and retry, NOT count as a circuit-breaker failure (which would open it for 60s and
        cascade the whole rebalance into skipped legs + failed cancels)."""
        msg = str(exc).lower()
        if "too many" in msg or "rate limit" in msg or "ratelimit" in msg:
            return True
        if "10006" in msg or "10018" in msg or "429" in msg:
            return True
        resp = getattr(exc, "response", None)
        return resp is not None and getattr(resp, "status_code", None) == 429

    def _req_semaphore(self) -> "asyncio.Semaphore":
        """Lazily create the in-flight-request semaphore so it binds to the running event loop."""
        if self._req_sem is None:
            self._req_sem = asyncio.Semaphore(self._max_concurrent_req)
        return self._req_sem

    async def _with_timestamp_retry(self, coro_fn, bypass_throttle: bool = False):
        """
        Run coro_fn() under the in-flight-request semaphore, with:
          - a single re-sync-and-retry on timestamp/recv_window errors, and
          - up to 3 backoff retries on Bybit rate-limit errors (429 / retCode 10006/10018).
        Rate limits must NOT reach the circuit breaker — the patient maker-chase fans out many
        concurrent calls, so a brief throttle should self-heal, not open the breaker.
        coro_fn: callable that returns a coroutine (e.g. _do when _do is async def)

        bypass_throttle: skip the entry-throttle semaphore entirely. Set for RISK-REDUCING calls
        (reduce-only closes) so a full-book maker-chase saturating the semaphore can NEVER starve an
        emergency flatten — closing the book must not queue behind opening it.
        """
        sem = self._req_semaphore()
        delay = 0.5
        resynced = False
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                if bypass_throttle:
                    return await coro_fn()        # safety lane: never wait on the entry-throttle semaphore
                async with sem:
                    return await coro_fn()
            except Exception as e:
                last_err = e
                _count_failure(e)
                if self._is_timestamp_error(e) and not resynced:
                    resynced = True
                    await self.sync_time()
                    continue                      # retry once after re-syncing the clock
                if self._is_rate_limit_error(e) and attempt < 3:
                    await asyncio.sleep(delay)     # 0.5s, 1s, 2s — let the throttle clear
                    delay *= 2
                    continue
                raise
        # Loop exhausted without returning (e.g. a timestamp error first surfacing on the FINAL attempt
        # hits `continue` with no attempt<4 guard). NEVER fall through to an implicit `return None`: a
        # None reads to callers as "call succeeded / order placed" (the circuit breaker even records it
        # as success), so a reduce-only CLOSE would look done while nothing was sent -> a naked position
        # believed flat. Re-raise the last error so the failure is loud.
        raise last_err or RuntimeError("bybit request: retries exhausted with no result")
    
    def _get_synced_timestamp(self) -> int:
        """
        Get current timestamp adjusted for server time offset.
        
        Returns:
            Timestamp in milliseconds, adjusted for server time offset
        """
        local_time = int(time.time() * 1000)
        return local_time + self._time_offset_ms
    
    def _generate_signature(
        self, 
        params: dict[str, Any], 
        timestamp: int, 
        recv_window: str = "5000",
        use_json_body: bool = False
    ) -> str:
        """
        Generate HMAC-SHA256 signature for Bybit API authentication.
        
        Bybit V5 API signature format:
        - GET requests: HMAC_SHA256(timestamp + api_key + recv_window + queryString)
        - POST requests: HMAC_SHA256(timestamp + api_key + recv_window + jsonBodyString)
        
        Args:
            params: Request parameters
            timestamp: Request timestamp in milliseconds
            recv_window: Receive window in milliseconds (default 5000)
            use_json_body: If True, use JSON body string format (for POST requests)
            
        Returns:
            Signature string
        """
        if use_json_body:
            # For POST requests: use JSON-serialized body string
            # Remove None values and convert to JSON without spaces, sorted keys
            # This must match EXACTLY what httpx sends
            clean_params = {k: v for k, v in params.items() if v is not None}
            json_body_string = json.dumps(clean_params, sort_keys=True, separators=(',', ':'))
            payload = f"{timestamp}{self.api_key}{recv_window}{json_body_string}"
        else:
            # For GET requests: use query string format
            # IMPORTANT: Use dict insertion order (not alphabetical sort).
            # httpx sends query params in dict insertion order. Bybit validates the
            # signature against the URL as-received, so the signing order MUST match.
            # Alphabetical sort broke when cursor pagination added params out-of-order.
            query_parts = []
            for k, v in params.items():
                if v is None:
                    continue
                # Convert boolean to lowercase string
                if isinstance(v, bool):
                    v = str(v).lower()
                else:
                    v = str(v)
                # URL encode the key and value
                encoded_key = urllib.parse.quote(str(k), safe='')
                encoded_value = urllib.parse.quote(v, safe='')
                query_parts.append(f"{encoded_key}={encoded_value}")
            
            query_string = "&".join(query_parts)
            payload = f"{timestamp}{self.api_key}{recv_window}{query_string}"
        
        # Generate HMAC-SHA256
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def _get_auth_headers(self, params: dict[str, Any], use_json_body: bool = False) -> dict[str, str]:
        """
        Generate authentication headers for Bybit API request.
        
        Uses synced timestamp to account for clock drift between local and server time.
        
        Args:
            params: Request parameters
            use_json_body: If True, use JSON body format for signature (POST requests)
            
        Returns:
            Headers dictionary with API key, timestamp, and signature
        """
        # Use synced timestamp to account for clock drift
        timestamp = self._get_synced_timestamp()
        recv_window = self._recv_window
        
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        
        # Generate signature (use JSON body format for POST requests)
        signature = self._generate_signature(params, timestamp, recv_window, use_json_body=use_json_body)
        headers["X-BAPI-SIGN"] = signature
        
        return headers
    
    async def list_instruments(
        self,
        category: str = "linear",
        symbol: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        List trading instruments (contracts) from Bybit.
        
        Args:
            category: Instrument category ("linear", "inverse", "spot", "option")
            symbol: Optional symbol filter (e.g., "BTCUSDT")
            status: Optional status filter ("Trading", "Settling", "Closed")
            
        Returns:
            List of instrument dictionaries
        """
        params: dict[str, Any] = {
            "category": category,
        }
        
        if symbol:
            params["symbol"] = symbol
        if status:
            params["status"] = status
        
        async def _do():
            await self._ensure_time_synced()
            headers = self._get_auth_headers(params)
            response = await self.client.get(
                "/v5/market/instruments-info",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("retCode") != 0:
                raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
            return data.get("result", {}).get("list", [])
        
        try:
            return await self._with_timestamp_retry(_do)
        except httpx.HTTPStatusError as e:
            raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
    
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        category: str = "linear",
        reduce_only: bool = False,
        close_on_trigger: bool = False,
        stop_loss: str | None = None,
        sl_trigger_by: str = "MarkPrice",
        order_link_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Place a market order on Bybit.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            side: Order side ("Buy" or "Sell")
            qty: Order quantity (as string to preserve precision)
            category: Instrument category ("linear", "inverse", "spot")
            reduce_only: Reduce-only order (default: False)
            close_on_trigger: Close position on trigger (default: False)
            stop_loss: Optional native stop-loss price (as string) attached ATOMICALLY at
                entry via /v5/order/create's `stopLoss` field. Default None = no change
                (callers that don't pass it are unaffected).
            sl_trigger_by: Trigger price type for the attached stop ("MarkPrice" default).

        Returns:
            Order response dictionary
        """
        async def _place_order():
            params: dict[str, Any] = {
                "category": category,
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": qty,
            }

            if reduce_only:
                params["reduceOnly"] = True
            if close_on_trigger:
                params["closeOnTrigger"] = True
            if stop_loss is not None:
                params["stopLoss"] = stop_loss
                params["slTriggerBy"] = sl_trigger_by
            if order_link_id:
                params["orderLinkId"] = order_link_id   # deterministic owner tag (per-deployment attribution)

            json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))
            
            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params, use_json_body=True)
                response = await self.client.post(
                    "/v5/order/create",
                    content=json_body_string,
                    headers={**headers, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                return data.get("result", {})
            
            try:
                # reduce-only = risk-reducing (a close/trim): jump the entry-throttle queue so an
                # emergency flatten can't be starved by a concurrent full-book maker-chase.
                return await self._with_timestamp_retry(_do, bypass_throttle=reduce_only)
            except httpx.HTTPStatusError as e:
                msg = f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}"
                await self._log_bybit_error(msg, {"endpoint": "place_order", "symbol": params.get("symbol")})
                raise Exception(msg)
            except Exception as e:
                msg = str(e) or f"Bybit API error: {type(e).__name__}: {e!r}"
                await self._log_bybit_error(msg, {"endpoint": "place_order", "symbol": params.get("symbol")})
                raise
        
        # A reduce-only order lowers risk: it is never blocked by an open breaker.
        return await self.circuit_breaker.call_async(_place_order, bypass_open=reduce_only)

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        price: str,
        category: str = "linear",
        post_only: bool = True,
        reduce_only: bool = False,
        order_link_id: str | None = None,
        stop_loss: str | None = None,
        sl_trigger_by: str = "MarkPrice",
    ) -> dict[str, Any]:
        """Place a LIMIT order for patient/maker execution (SmartExecutor child order).

        PostOnly by default -> maker-only: Bybit REJECTS (retCode 30076/110071) if it would cross the
        spread, so the caller re-prices instead of paying taker. qty/price are pre-formatted strings.
        `stop_loss` (pre-formatted string) attaches the native SL ATOMICALLY — it activates when the
        limit FILLS, so a patient fill is never naked (mandatory-SL compliance holds without a timer).
        """
        async def _place_limit():
            params: dict[str, Any] = {
                "category": category,
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": qty,
                "price": price,
                "timeInForce": "PostOnly" if post_only else "GTC",
            }
            if reduce_only:
                params["reduceOnly"] = True
            if order_link_id:
                params["orderLinkId"] = order_link_id
            if stop_loss is not None:
                params["stopLoss"] = stop_loss              # activates when the limit fills (atomic SL)
                params["slTriggerBy"] = sl_trigger_by
            json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))

            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params, use_json_body=True)
                response = await self.client.post(
                    "/v5/order/create",
                    content=json_body_string,
                    headers={**headers, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                return data.get("result", {})

            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                msg = f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}"
                await self._log_bybit_error(msg, {"endpoint": "place_limit", "symbol": symbol})
                raise Exception(msg)
            except Exception as e:
                msg = str(e) or f"Bybit API error: {type(e).__name__}: {e!r}"
                await self._log_bybit_error(msg, {"endpoint": "place_limit", "symbol": symbol})
                raise

        return await self.circuit_breaker.call_async(_place_limit)

    async def amend_order(
        self,
        symbol: str,
        order_id: str | None = None,
        order_link_id: str | None = None,
        price: str | None = None,
        qty: str | None = None,
        category: str = "linear",
    ) -> dict[str, Any]:
        """Amend an OPEN limit order's price/qty (/v5/order/amend) — re-price unfilled child ENTRY
        orders toward the touch as the deadline nears. NOTE: the move-only rule is for STOP-losses;
        working entry orders may be amended or cancel/replaced freely."""
        async def _amend():
            params: dict[str, Any] = {"category": category, "symbol": symbol}
            if order_id:
                params["orderId"] = order_id
            if order_link_id:
                params["orderLinkId"] = order_link_id
            if price is not None:
                params["price"] = str(price)
            if qty is not None:
                params["qty"] = str(qty)
            json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))

            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params, use_json_body=True)
                response = await self.client.post(
                    "/v5/order/amend",
                    content=json_body_string,
                    headers={**headers, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                return data.get("result", {})

            try:
                return await self._with_timestamp_retry(_do)
            except Exception as e:
                msg = str(e) or f"Bybit API error: {type(e).__name__}: {e!r}"
                # Benign during maker-chase: the resting order already filled before the amend landed
                # (the smart-executor catches this and re-prices). Log INFO, not alarming ERROR noise.
                _benign = is_benign_order_state(msg)
                await self._log_bybit_error(msg, {"endpoint": "amend_order", "symbol": symbol},
                                            level="INFO" if _benign else "ERROR")
                if _benign:
                    # Re-raise as the non-counting type so a WORKING chase cannot open the breaker.
                    # Same message text, so the executor's substring checks are unaffected.
                    raise BenignOrderStateError(msg) from e
                raise

        return await self.circuit_breaker.call_async(_amend)

    async def get_open_orders(
        self,
        symbol: str | None = None,
        category: str = "linear",
        settle_coin: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Open (unfilled / partially-filled) orders via /v5/order/realtime — for child-order fill
        tracking + crash-safe reconciliation. Returns the raw order list.

        PAGINATES. The API default page is 20 rows and this endpoint caps `limit` at 50 (unlike
        /v5/position/list, which allows 200). A patient book rebalance can rest dozens of post-only
        children concurrently, so an un-paginated single-page read would silently truncate the
        orphan-order sweep to the first 20 — leaving the rest resting on the exchange with no
        server-side expiry.
        """
        # Built ONCE, copied per attempt below.
        base: dict[str, Any] = {"category": category, "limit": min(max(1, limit), 50)}
        if symbol:
            base["symbol"] = symbol
        elif settle_coin:
            base["settleCoin"] = settle_coin

        async def _get_open():
            async def _do():
                await self._ensure_time_synced()
                # FRESH COPY PER ATTEMPT: _with_timestamp_retry re-runs _do, and a `cursor` left over
                # from a failed attempt would make the retry resume mid-book and silently drop page 1.
                params = dict(base)
                all_list: list[dict[str, Any]] = []
                cursor: str | None = None
                for _ in range(50):                      # safety bound, mirrors get_positions
                    if cursor is not None:
                        params["cursor"] = urllib.parse.unquote(cursor)
                    headers = self._get_auth_headers(params)
                    response = await self.client.get("/v5/order/realtime", params=params, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                    if data.get("retCode") != 0:
                        raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                    result = data.get("result", {})
                    page = result.get("list", [])
                    all_list.extend(page)
                    cursor = result.get("nextPageCursor") or None
                    if not cursor or not page:
                        break
                return all_list

            try:
                return await self._with_timestamp_retry(_do)
            except Exception as e:
                msg = str(e) or f"Bybit API error: {type(e).__name__}: {e!r}"
                await self._log_bybit_error(msg, {"endpoint": "get_open_orders", "symbol": symbol})
                raise

        return await self.circuit_breaker.call_async(_get_open)

    async def get_positions(
        self,
        category: str = "linear",
        symbol: str | None = None,
        settle_coin: str | None = None,
        limit: int = 200,
        bypass_breaker: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Get current positions from Bybit. Paginates automatically to return ALL
        positions (API default is 20 per page; we use limit=200 and cursor until done).
        
        Args:
            category: Instrument category ("linear", "inverse", "spot")
            symbol: Optional symbol filter (e.g., "BTCUSDT")
            settle_coin: Optional settlement coin filter (e.g., "USDT")
            limit: Max positions per request (1-200). Used for pagination.
            
        Returns:
            List of position dictionaries (all pages combined)
        """
        async def _do():
            params: dict[str, Any] = {
                "category": category,
                "limit": min(max(1, limit), 200),
            }
            if symbol:
                params["symbol"] = symbol
            if settle_coin:
                params["settleCoin"] = settle_coin

            await self._ensure_time_synced()
            all_list: list[dict[str, Any]] = []
            cursor: str | None = None
            max_pages = 50  # Safety: avoid infinite loop if API misbehaves

            for _ in range(max_pages):
                if cursor is not None:
                    params["cursor"] = urllib.parse.unquote(cursor)
                headers = self._get_auth_headers(params)
                response = await self.client.get(
                    "/v5/position/list",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                result = data.get("result", {})
                page = result.get("list", [])
                all_list.extend(page)
                cursor = result.get("nextPageCursor") or None
                if not cursor or not page:
                    break

            return all_list

        async def _get_positions():
            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                msg = f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}"
                log_symbol = symbol or ("all" if settle_coin else None)
                await self._log_bybit_error(msg, {"endpoint": "get_positions", "symbol": log_symbol})
                raise Exception(msg)
            except Exception as e:
                log_symbol = symbol or ("all" if settle_coin else None)
                msg = str(e) or f"Bybit API error: {type(e).__name__}: {e!r}"
                await self._log_bybit_error(msg, {"endpoint": "get_positions", "symbol": log_symbol})
                raise

        # bypass_breaker: a safety read (the drawdown guardian) must see the account even while the
        # breaker is open; it adds no risk.
        return await self.circuit_breaker.call_async(_get_positions, bypass_open=bypass_breaker)
    
    async def get_order(
        self,
        symbol: str,
        order_id: str | None = None,
        order_link_id: str | None = None,
        category: str = "linear",
    ) -> dict[str, Any] | None:
        """ONE order by id or by the relay's tag, via /v5/order/realtime: its state at once, also
        right after it filled or was cancelled (verified on the demo venue: /v5/order/history lags
        by seconds there, and does not list resting orders). None if the venue does not show it
        (e.g. an older order: use get_order_history)."""
        params: dict[str, Any] = {"category": category, "symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            params["orderLinkId"] = order_link_id

        async def _get():
            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params)
                response = await self.client.get("/v5/order/realtime", params=params, headers=headers)
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                return next((o for o in data.get("result", {}).get("list", [])
                             if (not order_id or o.get("orderId") == order_id)
                             and (not order_link_id or o.get("orderLinkId") == order_link_id)), None)
            return await self._with_timestamp_retry(_do)

        return await self.circuit_breaker.call_async(_get)

    async def get_order_history(
        self,
        category: str = "linear",
        symbol: str | None = None,
        order_id: str | None = None,
        limit: int = 20,
        order_link_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get order history including filled orders (`order_link_id` finds an order by the relay's
        own tag, e.g. to learn whether an order whose request timed out was accepted).
        
        This is essential for getting actual fill prices after placing market orders,
        since the order create response doesn't include avgPrice immediately.
        
        Args:
            category: Instrument category ("linear", "inverse", "spot")
            symbol: Optional symbol filter (e.g., "BTCUSDT")
            order_id: Optional specific order ID to query
            limit: Maximum number of orders to return (default 20, max 50)
            
        Returns:
            List of order dictionaries with:
            - orderId, symbol, side, orderType
            - avgPrice (actual fill price!)
            - cumExecQty (filled quantity)
            - orderStatus (New, PartiallyFilled, Filled, Cancelled, etc.)
            - createdTime, updatedTime
        """
        async def _get_order_history():
            params: dict[str, Any] = {
                "category": category,
                "limit": min(limit, 50),
            }
            
            if symbol:
                params["symbol"] = symbol
            if order_id:
                params["orderId"] = order_id
            if order_link_id:
                params["orderLinkId"] = order_link_id

            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params)
                response = await self.client.get(
                    "/v5/order/history",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                
                return data.get("result", {}).get("list", [])
            
            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                raise Exception(f"Bybit API error: {type(e).__name__}: {e!r}")
        
        return await self.circuit_breaker.call_async(_get_order_history)
    
    async def get_ticker(
        self,
        symbol: str,
        category: str = "linear",
    ) -> dict[str, Any]:
        """
        Get ticker data including 24h volume and current price.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            category: Instrument category ("linear", "inverse", "spot")
            
        Returns:
            Ticker dictionary with volume24h, lastPrice, etc.
        """
        async def _get_ticker():
            params: dict[str, Any] = {
                "category": category,
                "symbol": symbol,
            }
            
            # Public endpoint - no auth required
            try:
                response = await self.client.get(
                    "/v5/market/tickers",
                    params=params,
                )
                response.raise_for_status()
                
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                
                tickers = data.get("result", {}).get("list", [])
                if not tickers:
                    raise Exception(f"No ticker data found for {symbol}")
                
                ticker = tickers[0]
                
                if not isinstance(ticker, dict):
                    raise TypeError(f"Invalid ticker format for {symbol}: expected dict, got {type(ticker).__name__}")
                
                return ticker
                
            except httpx.HTTPStatusError as e:
                msg = f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}"
                await self._log_bybit_error(msg, {"endpoint": "get_ticker", "symbol": symbol})
                raise Exception(msg)
            except Exception as e:
                msg = str(e) or f"Bybit API error: {type(e).__name__}: {e!r}"
                await self._log_bybit_error(msg, {"endpoint": "get_ticker", "symbol": symbol})
                raise
        
        # Bound the hot-path touch call by the shared concurrency semaphore so a full-book
        # maker-chase fan-out (up to ~50 concurrent ticker GETs every chase interval) can't burst past
        # Bybit's per-IP limit and trip throttling — which would make get_ticker raise -> touch returns
        # (0,0) -> the chase amends the child to price "0".
        async with self._req_semaphore():
            return await _get_ticker()

    async def get_all_tickers(
        self,
        category: str = "linear",
        symbol: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        Get all ticker data in a single API call.
        
        Much more efficient than calling get_ticker() for each symbol individually.
        
        Args:
            category: Instrument category ("linear", "inverse", "spot")
            symbol: Optional symbol filter (e.g., "BTCUSDT") - if None, returns all tickers
            
        Returns:
            Dictionary mapping symbol -> ticker data
            Example: {"BTCUSDT": {"volume24h": "12345", "lastPrice": "50000", ...}, ...}
        """
        params: dict[str, Any] = {
            "category": category,
        }
        
        if symbol:
            params["symbol"] = symbol
        
        # Public endpoint - no auth required
        try:
            response = await self.client.get(
                "/v5/market/tickers",
                params=params,
            )
            response.raise_for_status()
            
            data = response.json()
            if data.get("retCode") != 0:
                raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
            
            tickers = data.get("result", {}).get("list", [])
            if not tickers:
                return {}
            
            # Convert list to dictionary keyed by symbol
            ticker_dict = {}
            for ticker in tickers:
                if isinstance(ticker, dict) and "symbol" in ticker:
                    ticker_dict[ticker["symbol"]] = ticker
            
            return ticker_dict
            
        except httpx.HTTPStatusError as e:
            raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise Exception(f"Bybit API error: {type(e).__name__}: {e!r}")
    
    async def get_account_balance(
        self,
        account_type: str = "UNIFIED",
        coin: str = "USDT",
        bypass_breaker: bool = False,
    ) -> float:
        """
        Fetch account balance from Bybit.
        
        Uses the Wallet Balance endpoint to get available balance.
        
        Args:
            account_type: Account type ("UNIFIED", "CONTRACT", "SPOT", "INVESTMENT", "OPTION", "FUND")
            coin: Coin to fetch balance for (default: "USDT")
            
        Returns:
            Total wallet balance in the specified coin
            
        Raises:
            Exception: If API call fails or balance not found
        """
        async def _get_balance():
            params: dict[str, Any] = {
                "accountType": account_type,
                "coin": coin,
            }
            
            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params)
                response = await self.client.get(
                    "/v5/account/wallet-balance",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                accounts = data.get("result", {}).get("list", [])
                if not accounts:
                    raise Exception(f"No account data found for {account_type}")
                account = accounts[0]
                total_equity = account.get("totalEquity", "0")
                if total_equity and float(total_equity) > 0:
                    return float(total_equity)
                # totalEquity==0 is a transient UNIFIED-account hiccup. Fall back ONLY to a
                # MARK-TO-MARKET equivalent — totalMarginBalance (= wallet + unrealized), or
                # totalWalletBalance + totalPerpUPL — NEVER per-coin walletBalance, which EXCLUDES
                # unrealized loss and would make the DD guardian UNDER-observe a drawdown (and the sizer
                # over-lever). If no MtM equity is available, FAIL CLOSED (raise): the guardian skips
                # the tick and the risk checks refuse, rather than trust a wrong-high number.
                mm = account.get("totalMarginBalance")
                if mm and float(mm) > 0:
                    logger.warning(f"[BybitClient] get_account_balance: totalEquity unavailable — using totalMarginBalance {mm} (MtM)")
                    return float(mm)
                wb = float(account.get("totalWalletBalance") or 0)
                upl_raw = account.get("totalPerpUPL")
                # Without the unrealised PnL the wallet balance alone would overstate equity during
                # a drawdown: then there is no usable mark-to-market number (fail closed below).
                upl = float(upl_raw) if upl_raw not in (None, "") else None
                if upl is not None and wb + upl > 0:
                    logger.info(f"[BybitClient] get_account_balance: using totalWalletBalance+UPL={wb + upl} (MtM)")
                    return float(wb + upl)
                raise Exception("get_account_balance: no usable mark-to-market equity (totalEquity/marginBalance/wallet all 0)")
            
            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
            except ValueError as e:
                raise Exception(f"Invalid balance format: {e!r}")
            except Exception as e:
                raise Exception(f"Bybit API error: {type(e).__name__}: {e!r}")

        return await self.circuit_breaker.call_async(_get_balance, bypass_open=bypass_breaker)
    
    async def get_api_key_info(self) -> dict[str, Any]:
        """What this key may do (GET /v5/user/query-api): readOnly, permissions, ips. Read by
        relay.venue.keycheck; never retried past the timestamp retry, never logged."""
        async def _do():
            await self._ensure_time_synced()
            headers = self._get_auth_headers({})
            response = await self.client.get("/v5/user/query-api", headers=headers)
            response.raise_for_status()
            data = response.json()
            if data.get("retCode") != 0:
                raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
            return data.get("result") or {}

        return await self._with_timestamp_retry(_do)

    async def get_usdt_trading_balance(self) -> tuple[float, str]:
        """
        Get USDT-margined trading balance (USDT + USDC usd_value only).
        
        Matches the frontend "Trading Balance (USDT)" card so the equity curve
        and the card show the same definition.
        
        Returns:
            Tuple of (balance, source). balance is sum of usdValue for coins
            USDT and USDC from wallet-balance. source is "wallet" or "fallback".
        """
        params: dict[str, Any] = {"accountType": "UNIFIED"}
        async def _do():
            await self._ensure_time_synced()
            headers = self._get_auth_headers(params)
            response = await self.client.get(
                "/v5/account/wallet-balance",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("retCode") != 0:
                raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
            accounts = data.get("result", {}).get("list", [])
            if not accounts:
                raise Exception("No account data found")
            account = accounts[0]
            total = 0.0
            for coin_data in account.get("coin", []):
                if coin_data.get("coin") in ("USDT", "USDC"):
                    usd_val = coin_data.get("usdValue", "0") or "0"
                    try:
                        total += float(usd_val)
                    except (ValueError, TypeError):
                        pass
            return (total, "wallet")
        try:
            return await self._with_timestamp_retry(_do)
        except Exception:
            return (0.0, "fallback")
    
    async def get_balance_safe(self, coin: str = "USDT") -> tuple[float, str]:
        """Account equity (mark-to-market, see get_account_balance) without raising.

        Returns (equity, 'wallet') when read, or (0.0, 'unavailable') when it could not be read
        (callers treat that as "unknown" and refuse, never as a real 0)."""
        try:
            return (await self.get_account_balance(coin=coin), "wallet")
        except Exception as e:
            logger.warning(f"[BybitClient] account equity unavailable: {e}")
            return (0.0, "unavailable")

    async def set_trading_stop(
        self,
        symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        tpsl_mode: str = "Full",
        tp_size: float | None = None,
        sl_size: float | None = None,
        tp_order_type: str = "Market",
        sl_order_type: str = "Market",
        position_idx: int = 0,
        category: str = "linear",
        sl_trigger_by: str = "MarkPrice",
        tp_trigger_by: str = "MarkPrice",
        trailing_stop: float | None = None,
        active_price: float | None = None,
    ) -> dict[str, Any]:
        """
        Set stop-loss and/or take-profit on an existing position.
        
        This is the correct way to manage SL/TP on Bybit positions.
        Uses the /v5/position/trading-stop endpoint.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            stop_loss: Stop loss price (None to not change)
            take_profit: Take profit price (None to not change)
            tpsl_mode: "Full" (entire position) or "Partial" (portion of position)
            tp_size: Contract quantity for partial TP (required when tpsl_mode="Partial")
            sl_size: Contract quantity for partial SL (must equal tp_size when Partial)
            tp_order_type: "Market" (default) or "Limit" - order type when TP triggers
            sl_order_type: "Market" (default) or "Limit" - order type when SL triggers
            position_idx: Position index:
                - 0: One-way mode (default)
                - 1: Buy side of hedge mode
                - 2: Sell side of hedge mode
            category: "linear" or "inverse"
            sl_trigger_by: Price type for SL trigger ("MarkPrice", "LastPrice", "IndexPrice")
            tp_trigger_by: Price type for TP trigger ("MarkPrice", "LastPrice", "IndexPrice")
            trailing_stop: trailing stop as a PRICE DISTANCE (not a percent); 0 cancels it. Full mode.
            active_price: price at which the trailing stop starts trailing (None = at once)
        
        Returns:
            API response dict with retCode, retMsg, result
        """
        async def _set_trading_stop():
            params: dict[str, Any] = {
                "category": category,
                "symbol": symbol,
                "positionIdx": position_idx,
                "tpslMode": tpsl_mode,
            }
            if stop_loss is not None:
                params["stopLoss"] = _num_str(stop_loss)
                params["slTriggerBy"] = sl_trigger_by
                params["slOrderType"] = sl_order_type
            if take_profit is not None:
                params["takeProfit"] = _num_str(take_profit)
                params["tpTriggerBy"] = tp_trigger_by
                params["tpOrderType"] = tp_order_type
            if trailing_stop is not None:
                params["trailingStop"] = _num_str(trailing_stop)
            if active_price is not None:
                params["activePrice"] = _num_str(active_price)
            if tpsl_mode == "Partial" and tp_size is not None and sl_size is not None:
                params["tpSize"] = _num_str(tp_size)
                params["slSize"] = _num_str(sl_size)
            json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))
            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params, use_json_body=True)
                response = await self.client.post(
                    "/v5/position/trading-stop",
                    content=json_body_string,
                    headers={**headers, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                ret_code = data.get("retCode", -1)
                ret_msg = data.get("retMsg", "Unknown error")
                if ret_code == 34040:
                    # "not modified": the requested values are already in place.
                    logger.info(f"[BybitClient] set_trading_stop for {symbol}: already in place")
                elif ret_code != 0:
                    # Make a rejected stop LOUD. Not raised: the caller decides (StopLossManager
                    # reports it; an entry without its stop is retried, then closed).
                    logger.warning(f"[BybitClient] set_trading_stop REJECTED for {symbol}: retCode={ret_code} "
                          f"{ret_msg} - the stop/take-profit was NOT changed")
                else:
                    logger.info(f"[BybitClient] set_trading_stop success for {symbol}: SL={stop_loss}, TP={take_profit}"
                                + (f", trailing={trailing_stop} from {active_price}" if trailing_stop is not None else ""))
                return data
            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                msg = f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}"
                await self._log_bybit_error(msg, {"endpoint": "set_trading_stop", "symbol": symbol})
                raise Exception(msg)
            except Exception as e:
                msg = str(e) or f"Bybit API error: {type(e).__name__}: {e!r}"
                await self._log_bybit_error(msg, {"endpoint": "set_trading_stop", "symbol": symbol})
                raise
        # Setting a protective stop lowers risk: never blocked by an open breaker.
        return await self.circuit_breaker.call_async(_set_trading_stop, bypass_open=True)
    
    async def set_leverage(
        self,
        symbol: str,
        leverage: float,
        category: str = "linear",
    ) -> dict[str, Any]:
        """
        Set leverage for a symbol (sent exactly: 2.5 stays 2.5).
        
        Uses the /v5/position/set-leverage endpoint.
        This should be called before opening a position to ensure correct leverage.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            leverage: Leverage multiplier (1-100 depending on symbol)
            category: "linear" or "inverse"
        
        Returns:
            API response dict with retCode, retMsg, result
        
        Note:
            - If leverage is already set to the requested value, Bybit returns retCode 110043
              with message "leverage not modified" - this is NOT an error
            - For one-way mode, buyLeverage and sellLeverage must be the same
        """
        async def _set_leverage():
            params: dict[str, Any] = {
                "category": category,
                "symbol": symbol,
                "buyLeverage": _num_str(leverage),
                "sellLeverage": _num_str(leverage),
            }
            json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))
            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params, use_json_body=True)
                response = await self.client.post(
                    "/v5/position/set-leverage",
                    content=json_body_string,
                    headers={**headers, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                ret_code = data.get("retCode", -1)
                ret_msg = data.get("retMsg", "Unknown error")
                if ret_code == 0:
                    logger.info(f"[BybitClient] set_leverage success for {symbol}: {leverage}x")
                elif ret_code == 110043:
                    logger.info(f"[BybitClient] set_leverage {symbol}: Already at {leverage}x (no change needed)")
                    return data
                else:
                    # FAIL CLOSED: a rejected leverage change (e.g. 110044 risk-limit, invalid leverage)
                    # means the symbol may be stuck at a HIGHER leverage → an entry placed now can
                    # liquidate INSIDE its stop. Raise so the caller aborts the entry.
                    # 110043 ("not modified") is handled above and is NOT an error.
                    logger.warning(f"[BybitClient] set_leverage REJECTED for {symbol}: {ret_msg} (aborting entry)")
                    raise Exception(f"set_leverage rejected for {symbol}: retCode={ret_code} {ret_msg}")
                return data
            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                raise Exception(f"Bybit API error: {type(e).__name__}: {e!r}")
        return await self.circuit_breaker.call_async(_set_leverage)

    async def switch_isolated_margin(
        self,
        symbol: str,
        leverage: int,
        isolated: bool = True,
        category: str = "linear",
    ) -> dict[str, Any]:
        """Switch a symbol between ISOLATED and CROSS margin.

        Uses /v5/position/switch-isolated. tradeMode 1=isolated, 0=cross. buyLeverage/sellLeverage
        must be provided and equal in one-way mode. retCode 110026 = "margin mode not modified"
        (already in that mode) is NOT an error.
        """
        async def _switch():
            params: dict[str, Any] = {
                "category": category,
                "symbol": symbol,
                "tradeMode": 1 if isolated else 0,
                "buyLeverage": _num_str(leverage),
                "sellLeverage": _num_str(leverage),
            }
            json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))

            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params, use_json_body=True)
                response = await self.client.post(
                    "/v5/position/switch-isolated",
                    content=json_body_string,
                    headers={**headers, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                ret_code = data.get("retCode", -1)
                if ret_code == 0:
                    logger.info(f"[BybitClient] switch_isolated_margin {symbol}: isolated={isolated} @ {leverage}x")
                elif ret_code == 110026:
                    logger.info(f"[BybitClient] switch_isolated_margin {symbol}: already {'isolated' if isolated else 'cross'}")
                else:
                    logger.warning(f"[BybitClient] switch_isolated_margin failed for {symbol}: {data.get('retMsg', 'Unknown error')}")
                return data

            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                raise Exception(f"Bybit API error: {type(e).__name__}: {e!r}")
        return await self.circuit_breaker.call_async(_switch)

    async def cancel_all_orders(
        self,
        category: str = "linear",
        symbol: str | None = None,
        settle_coin: str | None = None,
        base_coin: str | None = None,
    ) -> dict[str, Any]:
        """
        Cancel all open orders for a category, symbol, or settle coin.
        
        Uses the /v5/order/cancel-all endpoint.
        Priority: symbol > baseCoin > settleCoin
        
        Args:
            category: "linear", "inverse", "spot", or "option"
            symbol: Specific symbol to cancel orders for (optional)
            settle_coin: Settlement coin, e.g., "USDT" (optional)
            base_coin: Base coin, e.g., "BTC" (optional)
        
        Returns:
            API response with list of cancelled orders
        """
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        elif base_coin:
            params["baseCoin"] = base_coin
        elif settle_coin:
            params["settleCoin"] = settle_coin
        # Sign AND send the identical compact+sorted body (see cancel_order) — `json=` would mismatch.
        json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))

        async def _do():
            await self._ensure_time_synced()
            headers = self._get_auth_headers(params, use_json_body=True)
            response = await self.client.post(
                "/v5/order/cancel-all",
                content=json_body_string,
                headers={**headers, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            ret_code = data.get("retCode", -1)
            ret_msg = data.get("retMsg", "Unknown error")
            if ret_code != 0:
                logger.warning(f"[BybitClient] cancel_all_orders failed: {ret_msg}")
                raise Exception(f"Bybit API error {ret_code}: cancel_all_orders rejected: {ret_msg}")
            else:
                cancelled = data.get("result", {}).get("list", [])
                logger.info(f"[BybitClient] cancel_all_orders success: cancelled {len(cancelled)} orders")
            return data
        try:
            return await self._with_timestamp_retry(_do)
        except httpx.HTTPStatusError as e:
            raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise Exception(f"Bybit API error: {type(e).__name__}: {e!r}")
    
    async def cancel_order(
        self,
        symbol: str,
        order_id: str | None = None,
        order_link_id: str | None = None,
        category: str = "linear",
    ) -> dict[str, Any]:
        """Cancel ONE open order (/v5/order/cancel) — used by the SmartExecutor to pull a resting
        post-only child before the residual market-sweep. Idempotent enough: a 'not exists / already
        filled' retCode is swallowed by the caller (the order already left the book)."""
        params: dict[str, Any] = {"category": category, "symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            params["orderLinkId"] = order_link_id
        # Sign AND send the identical compact+sorted body (Bybit signs the raw bytes it receives).
        # httpx `json=` re-serializes with spaces/insertion-order → signature mismatch ('Error sign');
        # every other POST here uses this content= pattern, so cancel must too.
        json_body_string = json.dumps(params, sort_keys=True, separators=(',', ':'))

        async def _cancel():
            async def _do():
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params, use_json_body=True)
                response = await self.client.post(
                    "/v5/order/cancel",
                    content=json_body_string,
                    headers={**headers, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                return data.get("result", {})
            try:
                return await self._with_timestamp_retry(_do)
            except Exception as e:
                msg = f"cancel_order failed: {type(e).__name__}: {e!r}"
                # Benign during maker-chase: the order already filled before the cancel landed (the
                # executor catches this as 'already filled/gone'). Log INFO, not ERROR, to keep logs clean.
                _benign = is_benign_order_state(str(e))
                await self._log_bybit_error(msg, {"endpoint": "cancel_order", "symbol": symbol},
                                            level="INFO" if _benign else "ERROR")
                if _benign:
                    # Non-counting type: an order that FILLED before our cancel landed is the chase
                    # working, not the venue failing. Original message preserved for the executor.
                    raise BenignOrderStateError(str(e)) from e
                raise

        return await self.circuit_breaker.call_async(_cancel)

    async def get_closed_pnl(
        self,
        category: str = "linear",
        symbol: str | None = None,
        limit: int = 50,
        start_time: int | None = None,
        end_time: int | None = None,
        max_pages: int = 1,
    ) -> list[dict[str, Any]]:
        """
        Get closed PnL records from Bybit (newest first). `max_pages` > 1 follows the venue's
        cursor so a busy symbol's window is complete rather than cut at one page.
        
        Returns actual realized PnL including trading fees - the most accurate
        source for trade profitability.
        
        Args:
            category: "linear" or "inverse"
            symbol: Optional symbol filter (e.g., "BTCUSDT")
            limit: Max records to return (default 50, max 100)
            start_time: Optional start timestamp in milliseconds
            end_time: Optional end timestamp in milliseconds
        
        Returns:
            List of closed PnL records with:
            - symbol, side, qty, leverage
            - orderPrice, orderType
            - avgEntryPrice, avgExitPrice
            - closedPnl (actual realized PnL with fees!)
            - cumEntryValue, cumExitValue
            - createdTime, updatedTime
            - orderId, fillCount
        """
        async def _get_closed_pnl():
            params: dict[str, Any] = {
                "category": category,
                "limit": min(limit, 100),
            }
            if symbol:
                params["symbol"] = symbol
            if start_time:
                params["startTime"] = start_time
            if end_time:
                params["endTime"] = end_time
            async def _page(cursor: str | None):
                page_params = {**params, **({"cursor": cursor} if cursor else {})}
                await self._ensure_time_synced()
                headers = self._get_auth_headers(page_params)
                response = await self.client.get(
                    "/v5/position/closed-pnl",
                    params=page_params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                result = data.get("result", {})
                return result.get("list", []), result.get("nextPageCursor") or None

            async def _do():
                rows, cursor = [], None
                for _ in range(max(1, max_pages)):
                    page, cursor = await _page(cursor)
                    rows.extend(page)
                    if not cursor or not page:
                        break
                else:
                    if cursor:
                        logger.warning(f"[BybitClient] closed-pnl for {symbol}: more than {max_pages} pages; "
                                       f"older records not fetched")
                return rows
            try:
                return await self._with_timestamp_retry(_do)
            except httpx.HTTPStatusError as e:
                raise Exception(f"Bybit API HTTP error: {e.response.status_code} - {e.response.text}")
            except Exception as e:
                raise Exception(f"Bybit API error: {type(e).__name__}: {e!r}")
        return await self.circuit_breaker.call_async(_get_closed_pnl)

    async def get_transaction_log(
        self,
        category: str = "linear",
        start_time: int | None = None,
        end_time: int | None = None,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Itemized wallet cash-flow log (/v5/account/transaction-log), cursor-paginated to completion.
        The definitive source for decomposing an equity move: each entry has `type` (TRADE / SETTLEMENT
        = funding / TRANSFER_* / …), `change` (net wallet delta), `funding`, `fee`, `cashFlow`. Sum
        `change` by `type` to split realized-trade vs funding, and sum `fee` for total trading fees."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"accountType": "UNIFIED", "category": category, "limit": 50}
            if start_time:
                params["startTime"] = start_time
            if end_time:
                params["endTime"] = end_time
            if cursor is not None:
                params["cursor"] = urllib.parse.unquote(cursor)

            async def _do(params=params):   # bound now: this page's params
                await self._ensure_time_synced()
                headers = self._get_auth_headers(params)
                response = await self.client.get("/v5/account/transaction-log", params=params, headers=headers)
                response.raise_for_status()
                data = response.json()
                if data.get("retCode") != 0:
                    raise Exception(f"Bybit API error {data.get('retCode')}: {data.get('retMsg', 'Unknown error')}")
                return data.get("result", {}) or {}

            result = await self.circuit_breaker.call_async(lambda: self._with_timestamp_retry(_do))
            page = result.get("list", []) or []
            out.extend(page)
            cursor = result.get("nextPageCursor") or None
            if not cursor or not page:
                break
        return out

    async def close(self):
        """Close HTTP client connection."""
        await self.client.aclose()
    
    def __repr__(self) -> str:
        mode = "demo" if self.demo else ("testnet" if self.testnet else "mainnet")
        return f"<BybitRestClient(mode={mode}, base_url={self.base_url})>"

