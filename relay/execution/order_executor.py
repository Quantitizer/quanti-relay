"""
Order Executor - Enhanced order placement with retry logic and error handling.

Features:
- Retry logic for failed orders
- Slippage tolerance checking
- Error recovery and logging
- Dry-run mode support
- Circuit breaker awareness
"""
import asyncio
from typing import Any

from loguru import logger

from relay.exchange.bybit.rest_client import BybitRestClient
from relay.execution.dry_run import DryRunFlag, resolve_dry_run
from relay.util.circuit_breaker import CircuitBreakerOpen


def format_order_qty(quantity: float) -> str:
    """Plain decimal quantity string: never scientific notation ('1e-05') or float artifacts
    ('0.30000000000000004'), both of which venues reject. Quantities are already lot-aligned."""
    q = f"{quantity:.8f}".rstrip('0').rstrip('.')
    return q if '.' in q else f"{q}.0"

class OrderExecutor:
    """
    Executes orders on exchange with retry and error handling.
    
    Supports:
    - Market orders
    - Retry logic with exponential backoff
    - Slippage tolerance validation
    - Error recovery
    """
    
    def __init__(
        self,
        bybit_client: BybitRestClient,
        dry_run: DryRunFlag = True,
        retry_attempts: int = 3,
        retry_delay_sec: float = 2,
    ):
        """
        Initialize order executor.
        
        Args:
            bybit_client: Bybit REST client
            dry_run: Dry-run mode (no real orders) — bool or callable returning the current flag
            retry_attempts: attempts per order (exponential backoff between them)
            retry_delay_sec: base backoff delay
        """
        self.bybit = bybit_client
        self._dry_run = dry_run
        self.retry_attempts = retry_attempts
        self.retry_delay_sec = retry_delay_sec
    
    async def place_order(
        self,
        symbol: str,
        side: str,  # 'long' or 'short' -> 'Buy' or 'Sell'
        quantity: float,
        order_type: str = 'Market',
        expected_price: float | None = None,
        stop_loss_price: float | None = None,  # additive: attach a native SL atomically at entry
        order_link_id: str | None = None,  # owner tag (relay.execution.order_link) for import attribution
        simulate: bool | None = None,      # the entry's mode, decided once by the caller (None = flag)
    ) -> dict[str, Any]:
        """
        Place an order on the exchange.
        
        Args:
            symbol: Trading symbol
            side: 'long' or 'short'
            quantity: Order quantity
            order_type: Order type ('Market' only for now)
            expected_price: Expected fill price (for slippage check)
        
        Returns:
            Dict with keys:
                - success: bool
                - order_id: str | None
                - fill_price: float | None
                - fill_quantity: float | None
                - slippage_pct: float | None
                - error: str | None
        """
        dry_run_mode = simulate if simulate is not None else await resolve_dry_run(self._dry_run)
        
        if dry_run_mode:
            logger.info(
                f"[DRY-RUN] Would place {side.upper()} order: {quantity} {symbol} "
                f"@ expected ${expected_price or 0:.2f}"
            )
            return {
                'success': True,
                'order_id': f'dry_run_{symbol}_{side}',
                'fill_price': expected_price or 0.0,
                'fill_quantity': quantity,
                'slippage_pct': 0.0,
                'error': None,
            }
        
        retry_attempts = self.retry_attempts
        retry_delay_sec = self.retry_delay_sec
        
        # Convert side to Bybit format
        bybit_side = 'Buy' if side.lower() == 'long' else 'Sell'
        
        # Retry loop. An order is only ever re-sent when the venue is known NOT to have it: after an
        # ambiguous failure (timeout, dropped connection) it is first looked up by its tag, and a
        # re-send with the same tag is rejected by the venue as a duplicate, never filled twice.
        last_error = None
        for attempt in range(retry_attempts):
            try:
                qty_str = format_order_qty(quantity)

                # Optional native stop-loss attached atomically at entry.
                sl_str = None
                if stop_loss_price is not None and stop_loss_price > 0:
                    sl_str = f"{stop_loss_price:.10f}".rstrip('0').rstrip('.')

                order_result = await self.bybit.place_market_order(
                    symbol=symbol,
                    side=bybit_side,
                    qty=qty_str,
                    category='linear',
                    stop_loss=sl_str,
                    order_link_id=order_link_id,
                )
                return await self._entry_outcome(
                    symbol, side, quantity, expected_price,
                    order_id=order_result.get('orderId'), order_link_id=order_link_id,
                )

            except CircuitBreakerOpen as e:
                # Circuit breaker is open - fail fast, don't retry (nothing was sent)
                last_error = str(e)
                logger.error(
                    f"[OrderExecutor] Circuit breaker OPEN for {symbol} - failing immediately: {last_error}"
                )
                return {
                    'success': False,
                    'order_id': None,
                    'fill_price': None,
                    'fill_quantity': None,
                    'slippage_pct': None,
                    'error': last_error,
                    'circuit_breaker_open': True,
                }

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"[OrderExecutor] Order attempt {attempt + 1}/{retry_attempts} failed: {last_error}"
                )
                rejected_by_venue = last_error.startswith(('Bybit API error', 'Venue rejected'))
                if not rejected_by_venue or 'duplicate' in last_error.lower():
                    # Outcome unknown (or a duplicate tag: an earlier attempt WAS accepted). Ask the
                    # venue before doing anything else.
                    if order_link_id:
                        found = await self._fetch_fill(symbol, order_link_id=order_link_id, timeout=3.0)
                        if found is not None:
                            logger.warning(f"[OrderExecutor] {symbol}: the order was accepted despite the "
                                           f"error ({found['status']}) - not re-sending")
                            return await self._entry_outcome(
                                symbol, side, quantity, expected_price,
                                order_id=found.get('order_id'), order_link_id=order_link_id, known=found,
                            )
                    # Not found is not "not placed": the lookup can lag or fail, and some venues accept
                    # the same tag again once the first order has filled (it is unique among OPEN
                    # orders only). A re-send could double the position, so stop here and let the
                    # position import adopt it if it filled (with its stop: order_intents).
                    logger.error(f"[OrderExecutor] {symbol}: outcome unknown ({last_error}) - NOT re-sending; "
                                 f"the position import will adopt it if it filled")
                    return {'success': False, 'order_id': None, 'fill_price': None,
                            'fill_quantity': None, 'slippage_pct': None,
                            'error': f'outcome_unknown: {last_error}', 'outcome_unknown': True}

                # Don't retry on certain errors (insufficient balance, invalid params)
                if any(keyword in last_error.lower() for keyword in ['insufficient', 'invalid', 'not enough']):
                    logger.error(f"[OrderExecutor] Non-retryable error: {last_error}")
                    break

                # Wait before retry (exponential backoff)
                if attempt < retry_attempts - 1:
                    delay = retry_delay_sec * (2 ** attempt)  # Exponential backoff
                    logger.info(f"[OrderExecutor] Retrying in {delay}s...")
                    await asyncio.sleep(delay)

        # All retries failed
        logger.error(
            f"[OrderExecutor] ✗ Failed to place order after {retry_attempts} attempts: {last_error}"
        )

        return {
            'success': False,
            'order_id': None,
            'fill_price': None,
            'fill_quantity': None,
            'slippage_pct': None,
            'error': last_error,
        }

    async def _entry_outcome(
        self, symbol: str, side: str, quantity: float, expected_price: float | None,
        order_id: str | None, order_link_id: str | None,
        known: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The result of an accepted entry order, from the venue's own record of it: the real
        average price and filled quantity. When the venue cannot confirm in time, fill_price and
        fill_quantity are None and fill_source is 'unconfirmed' (the caller decides what to record
        and says so); nothing is filled in from the expected price here."""
        fill = known or await self._fetch_fill(symbol, order_id=order_id, order_link_id=order_link_id)
        if fill is not None and fill['status'] == 'Filled' and not fill.get('qty'):
            fill = {**fill, 'qty': quantity}      # 'Filled' = the whole order quantity executed
        if fill is not None and not fill.get('qty'):
            # Final with nothing executed (cancelled / rejected after acceptance).
            logger.error(f"[OrderExecutor] {symbol}: order {order_id} ended {fill['status']} with nothing filled")
            return {'success': False, 'order_id': order_id, 'fill_price': None, 'fill_quantity': None,
                    'slippage_pct': None, 'error': f"order {fill['status']} with nothing filled"}
        if fill is None or not fill.get('avg_price'):
            logger.warning(f"[OrderExecutor] {symbol}: order {order_id} accepted, fill not yet confirmed")
            return {'success': True, 'order_id': order_id, 'fill_price': None,
                    'fill_quantity': (fill or {}).get('qty'), 'slippage_pct': None, 'error': None,
                    'fill_source': 'unconfirmed'}

        fill_price, fill_qty = fill['avg_price'], fill['qty']
        if fill_qty < quantity - 1e-12:
            logger.warning(f"[OrderExecutor] {symbol}: PARTIAL fill {fill_qty} of {quantity} ({fill['status']})")
        slippage_pct = None
        if expected_price:
            slippage_pct = abs((fill_price - expected_price) / expected_price * 100)
        logger.info(f"[OrderExecutor] ✓ Placed {side.upper()} order: {fill_qty} {symbol} "
                    f"@ ${fill_price:.2f} (Order: {order_id})")
        return {'success': True, 'order_id': order_id, 'fill_price': fill_price,
                'fill_quantity': fill_qty, 'slippage_pct': slippage_pct, 'error': None,
                'fill_source': 'venue_order'}
    
    # Venue order states that are final (nothing more will fill).
    _TERMINAL = frozenset({'Filled', 'Cancelled', 'PartiallyFilledCanceled', 'Rejected', 'Deactivated'})

    async def _fetch_fill(
        self,
        symbol: str,
        order_id: str | None = None,
        order_link_id: str | None = None,
        timeout: float = 5.0,
        poll_interval: float = 0.5,
        client=None,
    ) -> dict[str, Any] | None:
        """
        The venue's own record of an order's outcome: {'status', 'avg_price', 'qty'} once the
        order is final, or None if it could not be confirmed within `timeout` (or was not found).

        The create-order response carries no fill data, so this is the only source of the REAL
        entry price and filled quantity. A market order can end partly filled
        ('PartiallyFilledCanceled', or 'Cancelled' with executions): the filled part is real.
        """
        _c = client or self.bybit
        for _ in range(max(1, int(timeout / poll_interval))):
            try:
                orders = await _c.get_order_history(
                    symbol=symbol, order_id=order_id, order_link_id=order_link_id,
                    category='linear', limit=1,
                )
                if orders:
                    order = orders[0]
                    status = order.get('orderStatus', '')
                    if status in self._TERMINAL:
                        qty = order.get('cumExecQty')
                        if qty in (None, '') and status == 'Filled':
                            qty = order.get('qty')
                        avg = float(order.get('avgPrice') or 0) or None
                        return {'status': status, 'avg_price': avg,
                                'qty': float(qty) if qty not in (None, '') else None,
                                'order_id': order.get('orderId')}
            except Exception as e:
                logger.warning(f"[OrderExecutor] Error checking order status: {e}")
            await asyncio.sleep(poll_interval)
        logger.warning(f"[OrderExecutor] {symbol}: could not confirm order "
                       f"{order_id or order_link_id} within {timeout}s")
        return None


    async def _get_current_price(self, symbol: str, client=None) -> float:
        """
        Get current market price for a symbol as fallback.

        Args:
            symbol: Trading symbol
            client: optional per-account client/adapter (defaults to self.bybit)

        Returns:
            Current market price, or 0 if unable to fetch
        """
        try:
            _c = client or self.bybit
            ticker = await _c.get_ticker(symbol=symbol, category='linear')
            return float(ticker.get('lastPrice', 0))
        except Exception as e:
            logger.warning(f"[OrderExecutor] Error fetching current price for {symbol}: {e}")
            return 0
    
    async def close_position(
        self,
        symbol: str,
        side: str,  # 'long' or 'short' -> 'Sell' or 'Buy'
        quantity: float,
        order_type: str = 'Market',
        client=None,
        simulate: bool | None = None,
    ) -> dict[str, Any]:
        """
        Close a position with market order.

        Args:
            symbol: Trading symbol
            side: 'long' or 'short' (position side, not order side)
            quantity: Position quantity to close
            order_type: Order type ('Market' only)
            client: optional per-account Bybit client/adapter. Defaults to self.bybit (the DEFAULT
                account). A keyed sub-account MUST pass its own adapter so the close order,
                fill polling and price fallback all hit that account — not the default one.
            simulate: the POSITION's own mode: True = a paper position (simulated close, nothing
                sent), False = a real position (a real close, whatever the current flag says).
                None = the executor's current dry-run flag. Callers closing a known position pass
                its `dry_run`, so a mode switch can never close a paper row for real (or the reverse).

        Returns:
            Dict with keys:
                - success: bool
                - order_id: str | None
                - fill_price: float | None
                - fill_quantity: float | None
                - error: str | None
        """
        _c = client or self.bybit   # multi-account: route to the position's own account when given
        dry_run_mode = simulate if simulate is not None else await resolve_dry_run(self._dry_run)

        if dry_run_mode:
            # Paper close at the current market price; if no price can be read, the fill is
            # unknown (None), never 0 (which would read as a -100% loss).
            current_price = await self._get_current_price(symbol, client=_c) or None
            if current_price is None:
                # A simulated close needs a price for its fill: refuse rather than record a close
                # whose result is unknown. The position stays open; the close can be retried.
                logger.warning(f"[DRY-RUN] {symbol}: no market price to simulate the close - not closed")
                return {'success': False, 'order_id': None, 'fill_price': None, 'fill_quantity': None,
                        'error': 'no market price to simulate the paper close'}
            logger.info(
                f"[DRY-RUN] Would close {side.upper()} position: {quantity} {symbol} "
                f"@ current market {current_price}"
            )
            return {
                'success': True,
                'order_id': f'dry_run_close_{symbol}_{side}',
                'fill_price': current_price,
                'fill_quantity': quantity,
                'error': None,
            }
        
        # Convert position side to order side (opposite)
        bybit_side = 'Sell' if side.lower() == 'long' else 'Buy'
        
        retry_attempts = self.retry_attempts
        retry_delay_sec = self.retry_delay_sec
        
        # Retry loop
        last_error = None
        for attempt in range(retry_attempts):
            try:
                # Place reduce-only market order
                order_result = await _c.place_market_order(
                    symbol=symbol,
                    side=bybit_side,
                    qty=format_order_qty(quantity),
                    category='linear',
                    reduce_only=True,
                )

                order_id = order_result.get('orderId')

                # The create response carries no fill: read the venue's record of the order. If it
                # cannot be confirmed in time the fill price is None (the close still happened; the
                # PnL reconciler prices it later) - never the ticker, never 0.
                fill = await self._fetch_fill(symbol, order_id=order_id, client=_c) if order_id else None
                fill_price = (fill or {}).get('avg_price')
                fill_qty = (fill or {}).get('qty')
                if fill is not None and not fill_qty and fill['status'] != 'Filled':
                    raise Exception(f"close order {order_id} ended {fill['status']} with nothing filled")
                logger.info(
                    f"[OrderExecutor] ✓ Closed {side.upper()} position: {quantity} {symbol} "
                    f"@ {fill_price if fill_price else 'unconfirmed price'} (Order: {order_id})"
                )
                return {
                    'success': True,
                    'order_id': order_id,
                    'fill_price': fill_price,
                    'fill_quantity': fill_qty or (quantity if fill is not None else None),
                    'error': None,
                }
            
            except CircuitBreakerOpen as e:
                # Circuit breaker is open - fail fast, don't retry
                # The caller should handle circuit breaker state
                last_error = str(e)
                logger.error(
                    f"[OrderExecutor] Circuit breaker OPEN while closing {symbol} - failing immediately: {last_error}"
                )
                return {
                    'success': False,
                    'order_id': None,
                    'fill_price': None,
                    'fill_quantity': None,
                    'error': last_error,
                    'circuit_breaker_open': True,
                }
            
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"[OrderExecutor] Close attempt {attempt + 1}/{retry_attempts} failed: {last_error}"
                )
                
                # Don't retry on certain errors
                if any(keyword in last_error.lower() for keyword in ['invalid', 'not found', 'no position']):
                    logger.error(f"[OrderExecutor] Non-retryable error: {last_error}")
                    break
                
                # Wait before retry
                if attempt < retry_attempts - 1:
                    delay = retry_delay_sec * (2 ** attempt)
                    logger.info(f"[OrderExecutor] Retrying close in {delay}s...")
                    await asyncio.sleep(delay)
        
        # All retries failed
        logger.error(
            f"[OrderExecutor] ✗ Failed to close position after {retry_attempts} attempts: {last_error}"
        )
        
        return {
            'success': False,
            'order_id': None,
            'fill_price': None,
            'fill_quantity': None,
            'error': last_error,
        }

    async def close_position_fast(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client=None,
        simulate: bool | None = None,
    ) -> dict[str, Any]:
        """
        Close a position with market order - FAST mode for bulk closes.

        This method places the order and returns immediately WITHOUT waiting for:
        - Fill price confirmation (fetched later in batch)
        - Order history polling

        The fill is read afterwards (the engine's finalize step), so bulk closes are not serialised
        behind fill confirmations.

        Args:
            symbol: Trading symbol
            side: 'long' or 'short' (position side, not order side)
            quantity: Position quantity to close
            client: optional per-account client/adapter (defaults to self.bybit)
            simulate: the position's own mode (see close_position). The guardian's flatten of a
                LIVE account passes False: a safety flatten is never silenced by a stale flag.

        Returns:
            Dict with keys:
                - success: bool
                - order_id: str | None
                - error: str | None
        """
        _c = client or self.bybit   # multi-account: route to the position's own account when given
        dry_run_mode = simulate if simulate is not None else await resolve_dry_run(self._dry_run)

        if dry_run_mode:
            logger.info(
                f"[DRY-RUN] Would close {side.upper()} position: {quantity} {symbol}"
            )
            return {
                'success': True,
                'order_id': f'dry_run_close_{symbol}_{side}',
                'error': None,
            }

        # Convert position side to order side (opposite)
        bybit_side = 'Sell' if side.lower() == 'long' else 'Buy'

        try:
            # Place reduce-only market order - returns immediately
            order_result = await _c.place_market_order(
                symbol=symbol,
                side=bybit_side,
                qty=format_order_qty(quantity),
                category='linear',
                reduce_only=True,
            )
            
            order_id = order_result.get('orderId')
            
            logger.info(
                f"[OrderExecutor] ✓ Fast close order placed: {side.upper()} {quantity} {symbol} "
                f"(Order: {order_id})"
            )
            
            return {
                'success': True,
                'order_id': order_id,
                'error': None,
            }
            
        except CircuitBreakerOpen as e:
            logger.error(f"[OrderExecutor] Circuit breaker OPEN during fast close of {symbol}")
            return {
                'success': False,
                'order_id': None,
                'error': str(e),
                'circuit_breaker_open': True,
            }
            
        except Exception as e:
            logger.error(f"[OrderExecutor] Fast close failed for {symbol}: {e}")
            return {
                'success': False,
                'order_id': None,
                'error': str(e),
            }