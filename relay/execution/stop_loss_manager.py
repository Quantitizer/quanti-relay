"""
Stop-Loss Manager — validates stop prices and sets/updates/cancels position stops on Bybit.

Stop prices come from the signal (or the deployment's default stop distance); this module never
invents a stop. It only guards them:
- a stop on the wrong side of the CURRENT price (would trigger immediately) is corrected,
- min/max distance from entry are enforced,
- tiny moves are skipped to avoid churn and rate limits.
"""
from typing import Any

from loguru import logger

from relay.exchange.bybit.rest_client import BybitRestClient
from relay.execution.dry_run import DryRunFlag, resolve_dry_run


class StopLossManager:
    """Validates stop prices and manages position-attached stops (Bybit trading-stop)."""

    def __init__(
        self,
        bybit_client: BybitRestClient,
        dry_run: DryRunFlag = True,
    ):
        """
        Args:
            bybit_client: Bybit REST client (or a per-account adapter with the same methods)
            dry_run: bool, or a (sync/async) callable returning the CURRENT dry-run flag so a
                runtime switch takes effect without rebuilding the manager. Defaults to True.
        """
        self.bybit = bybit_client
        self._dry_run = dry_run

    async def _is_dry_run(self) -> bool:
        return await resolve_dry_run(self._dry_run)

    def validate_stop(
        self,
        side: str,
        entry_price: float,
        stop_price: float,
        current_price: float | None = None,
        min_distance_pct: float | None = None,
        max_distance_pct: float | None = None,
    ) -> dict[str, Any]:
        """
        Check a stop WITHOUT changing it. The relay never invents or moves a stop on its own.

        - Already breached: a long stop at/above the reference price (current price, else entry),
          or a short stop at/below it, would trigger at once -> valid=False,
          issue='stop_already_breached'. The caller decides (at entry: close, since the stop would
          have fired; in the audit: report).
        - Distance limits are the user's optional settings: a stop closer than min_distance_pct or
          further than max_distance_pct (from entry) is reported in 'warnings', not altered.

        Returns {'stop_price', 'valid', 'issue', 'distance_pct', 'warnings'}.
        """
        reference = current_price if current_price else entry_price
        long_side = str(side).lower() in ('long', 'buy')
        breached = stop_price >= reference if long_side else stop_price <= reference
        distance_pct = (abs(entry_price - stop_price) / entry_price * 100) if entry_price > 0 else 0.0
        warnings = []
        if min_distance_pct is not None and distance_pct < min_distance_pct:
            warnings.append(f'below_min_distance ({distance_pct:.3f}% < {min_distance_pct}%)')
        if max_distance_pct is not None and distance_pct > max_distance_pct:
            warnings.append(f'beyond_max_distance ({distance_pct:.3f}% > {max_distance_pct}%)')
        if breached:
            logger.error(
                f"[StopLoss] {side} stop {stop_price:.6f} is already breached at {reference:.6f} "
                f"(entry {entry_price:.6f}) - it would trigger immediately; NOT moved"
            )
        for w in warnings:
            logger.warning(f"[StopLoss] {side} stop {stop_price:.6f}: {w} - kept as given")
        return {
            'stop_price': stop_price,
            'valid': not breached,
            'issue': 'stop_already_breached' if breached else None,
            'distance_pct': round(distance_pct, 4),
            'warnings': warnings,
        }

    async def place_stop_loss(
        self,
        symbol: str,
        side: str,  # 'long' or 'short'
        quantity: float,
        stop_price: float,
        take_profit: float | None = None,
        simulate: bool | None = None,
    ) -> dict[str, Any]:
        """
        Set the position's stop-loss (venue-side trading stop), then, separately, its take-profit.

        The stop goes first and alone, so a take-profit the venue rejects can never take the stop
        down with it. A take-profit failure is reported in 'take_profit_error'; the result's
        'success' is about the stop.

        Returns {'order_id', 'success', 'error', 'unchanged'?, 'take_profit_error'?}.
        """
        dry_run_mode = simulate if simulate is not None else await self._is_dry_run()

        if dry_run_mode:
            msg = f"[DRY-RUN] Would set {side} stop @ ${stop_price:,.2f} for {symbol}"
            if take_profit is not None:
                msg += f" and TP @ ${take_profit:,.2f}"
            logger.info(msg)
            return {
                'order_id': f'dry_run_stop_{symbol}',
                'success': True,
                'error': None,
            }

        try:
            response = await self.bybit.set_trading_stop(
                symbol=symbol,
                stop_loss=stop_price,
                tpsl_mode='Full',
                position_idx=0,  # One-way mode
                category='linear',
                sl_trigger_by='MarkPrice',
            )
            result = self._trading_stop_result(response, f"{side} stop @ ${stop_price:,.2f} for {symbol}", symbol)
        except Exception as e:
            logger.error(f"[StopLoss] Exception setting stop-loss: {e}")
            return {'order_id': None, 'success': False, 'error': str(e)}

        if result['success'] and take_profit is not None:
            tp = await self.place_take_profit(symbol, side, take_profit, simulate=simulate)
            if not tp['success']:
                result['take_profit_error'] = tp['error']
        return result

    async def place_take_profit(self, symbol: str, side: str, take_profit: float,
                                simulate: bool | None = None) -> dict[str, Any]:
        """Set (only) the position's full take-profit. Leaves any stop untouched."""
        if simulate if simulate is not None else await self._is_dry_run():
            logger.info(f"[DRY-RUN] Would set {side} TP @ ${take_profit:,.2f} for {symbol}")
            return {'order_id': f'dry_run_tp_{symbol}', 'success': True, 'error': None}
        try:
            response = await self.bybit.set_trading_stop(
                symbol=symbol,
                take_profit=take_profit,
                tpsl_mode='Full',
                position_idx=0,
                category='linear',
            )
            return self._trading_stop_result(response, f"{side} TP @ ${take_profit:,.2f} for {symbol}", symbol)
        except Exception as e:
            logger.error(f"[StopLoss] Exception setting take-profit: {e}")
            return {'order_id': None, 'success': False, 'error': str(e)}

    async def set_trailing_stop(self, symbol: str, side: str, distance: float,
                                active_price: float | None = None,
                                simulate: bool | None = None) -> dict[str, Any]:
        """Set (distance > 0) or cancel (distance 0) the position's trailing stop: it follows the
        best price at `distance` (a PRICE distance), from `active_price` on (None = at once). Bybit
        keeps it beside the fixed stop, which this never touches (verified on the demo venue)."""
        what = (f"{side} trailing stop {distance:g}" + (f" from {active_price:g}" if active_price else "")
                if distance else f"{side} trailing stop cancel")
        if simulate if simulate is not None else await self._is_dry_run():
            logger.info(f"[DRY-RUN] Would set {what} for {symbol}")
            return {'order_id': f'dry_run_trailing_{symbol}', 'success': True, 'error': None}
        try:
            response = await self.bybit.set_trading_stop(
                symbol=symbol, tpsl_mode='Full', position_idx=0, category='linear',
                trailing_stop=distance, active_price=active_price)
            return self._trading_stop_result(response, f"{what} for {symbol}", symbol)
        except Exception as e:
            logger.error(f"[StopLoss] Exception setting trailing stop: {e}")
            return {'order_id': None, 'success': False, 'error': str(e)}

    @staticmethod
    def _trading_stop_result(response: dict[str, Any], what: str, symbol: str) -> dict[str, Any]:
        ret_code = response.get('retCode', -1)
        ret_msg = response.get('retMsg', 'Unknown error')
        if ret_code == 0:
            logger.info(f"[StopLoss] Set {what}")
            return {'order_id': f'trading_stop_{symbol}', 'success': True, 'error': None}
        # "Not modified" (Bybit 34040): the requested value is ALREADY in place, so this is a
        # success. Never clear-and-reset: that would leave the position without a stop for the gap
        # between the two calls, and with none at all if the second call failed.
        if ret_code == 34040 or 'not modified' in str(ret_msg).lower():
            logger.info(f"[StopLoss] {what}: already in place")
            return {'order_id': f'trading_stop_{symbol}', 'success': True, 'error': None, 'unchanged': True}
        logger.error(f"[StopLoss] Failed to set {what}: {ret_msg}")
        return {'order_id': None, 'success': False, 'error': ret_msg}

    async def update_stop_loss(
        self,
        position_id: int,
        old_order_id: str | None,
        new_stop_price: float,
        symbol: str,
        side: str,
        quantity: float,
    ) -> dict[str, Any]:
        """
        Update existing stop-loss on position.
        
        Note: Uses set_trading_stop() which overwrites the previous SL on the position.
        No need to cancel the old stop - it's simply overwritten.
        
        Args:
            position_id: Position ID
            old_order_id: Existing stop-loss order ID (unused, kept for interface compatibility)
            new_stop_price: New stop-loss price
            symbol: Trading symbol
            side: Position side
            quantity: Position quantity
        
        Returns:
            Dict with keys:
                - order_id: str | None
                - success: bool
                - error: str | None
        """
        # Note: No need to cancel old stop - set_trading_stop() overwrites the previous SL
        # The old_order_id parameter is kept for interface compatibility but not used
        
        return await self.place_stop_loss(
            symbol=symbol,
            side=side,
            quantity=quantity,
            stop_price=new_stop_price,
        )
    
    async def cancel_stop_loss(self, order_id: str, symbol: str, client=None) -> bool:
        """
        Cancel stop-loss order.

        Args:
            order_id: Order ID to cancel
            symbol: Symbol the order rests on (Bybit's cancel endpoint requires it)
            client: optional per-account client/adapter. Defaults to self.bybit (DEFAULT account). A
                keyed sub-account's stop MUST be cancelled on its OWN account. (Position-attached
                `trading_stop_` stops short-circuit below and never reach the API.)

        Returns:
            True if successful
        """
        # Synthetic trading-stop IDs: Bybit clears them when position closes; no cancel needed
        if order_id and str(order_id).startswith("trading_stop_"):
            return True
        if await self._is_dry_run() or str(order_id or '').startswith('dry_run_'):
            # A paper position's stop id never existed on the venue, whatever the current flag says.
            logger.info(f"[DRY-RUN] Would cancel stop order {order_id}")
            return True

        try:
            # The venue needs the symbol to cancel an order by id.
            await (client or self.bybit).cancel_order(symbol=symbol, order_id=order_id)
            logger.info(f"[StopLoss] Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"[StopLoss] Failed to cancel order: {e!s}")
            return False
