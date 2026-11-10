"""Event executor: one v1 event signal (relay.signals.contract) carried out on the engine.

  open       a new position (ExecutionEngine.execute_entry, with every entry check)
  close      the symbol's position
  reduce     part of it (size.close_pct of its current size)
  flip       close the symbol's position on the other side, then open `side` (the user's cooldown
             after a close does not stop the second half; a failed close stops the open)
  close_all  every position of the deployment in the signal's mode
  move_stop  the symbol's position's stop to stop.price

The signal's mode is paper when the deployment is in dry-run or the signal says dry_run: true (a
signal can only be more cautious). It only ever acts on positions of its own mode: a dry-run signal
never closes, reduces or moves the stop of a live position.

The outcome is recorded on the signal row: executed, skipped (refused, with the reason) or failed
(the venue or the relay could not do it). An open records its own verdict (the entry path).
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from loguru import logger

from relay.execution.error_messages import humanize_order_error
from relay.signals.contract import EventSignal, symbol_parts


def bybit_symbol(unified: str) -> str:
    """The Bybit linear symbol of a unified one (BTC/USDT:USDT -> BTCUSDT). Only USDT-settled
    perpetuals so far; anything else raises ValueError."""
    base, quote, settle = symbol_parts(unified)
    if quote != "USDT" or settle != "USDT":
        raise ValueError(f"{unified}: only USDT-settled perpetuals are supported on Bybit so far")
    return f"{base}USDT"


class EventExecutor:
    def __init__(self, engine, signal_log, events=None):
        self.engine = engine
        self.signal_log = signal_log
        self.events = events            # relay.events.EventLog: the outcome is also an event

    async def execute(self, sig: EventSignal, row_id: int) -> dict[str, Any]:
        """Carry out `sig` (already authenticated, validated and logged as row `row_id`). Never
        raises: every outcome is returned and recorded."""
        try:
            result = await self._execute(sig, row_id)
        except asyncio.CancelledError:
            # Cut off (a shutdown that could not wait any longer): never left 'accepted'.
            await self._record(row_id, sig, {'success': False, 'status': 'failed', 'reason': 'interrupted',
                                             'error': 'cut off at shutdown - check the position on the venue'})
            raise
        except Exception as e:
            logger.opt(exception=True).error(f"[Signal] #{row_id} {sig.action} {sig.symbol}: {e}")
            result = {'success': False, 'status': 'failed', 'reason': 'internal_error', 'error': str(e)}
        await self._record(row_id, sig, result)
        return result

    async def _record(self, row_id: int, sig: EventSignal, result: dict[str, Any]) -> None:
        status = result.get('status') or ('executed' if result.get('success') else 'skipped')
        reasons = [result['reason']] if result.get('reason') else None
        try:
            if sig.action in ('open', 'flip') and not result.get('status'):
                # The entry path recorded its own verdict on the row: keep it, add the result.
                # One it could not record (e.g. its own write failed) stays 'accepted': failed.
                row = await self.signal_log.get(row_id)
                if row is not None and row.status != 'accepted':
                    status, reasons = row.status, row.reasons
                elif not result.get('success'):
                    status = 'failed'

            await self.signal_log.mark_result(row_id, status, reasons=reasons, result=_jsonable(result))
        except Exception as e:
            logger.error(f"[Signal] #{row_id}: outcome {status} could not be recorded: {e}")
        await signal_event(self.events, row_id, sig.source, f"{sig.action} {sig.symbol or 'all'}", status, reasons)

    async def _execute(self, sig: EventSignal, row_id: int) -> dict[str, Any]:
        paper = await self.engine.get_dry_run_mode() or sig.dry_run is True
        symbol = None
        if sig.symbol is not None:
            try:
                symbol = bybit_symbol(sig.symbol)
            except ValueError as e:
                return _skip('unsupported_symbol', str(e))
        if sig.execution is not None and sig.execution.type != 'market':
            return _skip('execution_not_supported',
                         f'execution {sig.execution.type} is not supported yet - send market (or omit it)')

        if sig.action == 'close_all':
            return await self._close_all(paper)
        positions = await self._positions(symbol, paper)
        if sig.action == 'open':
            return await self._open(sig, symbol, paper, row_id)
        if sig.action == 'flip':
            same = [p for p in positions if p['side'].lower() == sig.side]
            if same:
                return _skip('already_that_side', f'{symbol} is already {sig.side}: nothing to flip')
            # Refused before anything is closed: the new side would be refused after it.
            venue = self.engine.bybit
            if sig.side == 'short' and not getattr(venue, 'can_short', True):
                return _skip('short_unsupported', 'this venue is long only (spot): a flip to short is refused')
            if sig.stop is not None and not getattr(venue, 'can_stop', True):
                return _skip('stop_unsupported', 'this venue cannot hold a stop order: send the flip without one')
            closed = []
            for p in positions:
                res = await self._close(p)
                if not res['success']:
                    return {**res, 'action': 'flip', 'closed_position_ids': closed,
                            'note': f'the {sig.side} side was not opened'}
                if res.get('close') == 'closed':     # 'not_open': it closed by itself meanwhile
                    closed.append(p['id'])
            res = {**await self._open(sig, symbol, paper, row_id, flip=bool(closed)), 'closed_position_ids': closed}
            if closed and not res.get('success'):
                row = await self.signal_log.get(row_id)
                if row is not None and row.status == 'skipped':
                    # Half done: the old side IS closed (an order filled), the new one was refused.
                    res.update(status='executed', reason=f"flip_open_refused: {res.get('reason')}")
                # Otherwise the entry's own verdict stands (e.g. it filled but its stop failed).
            return res

        if sig.side is not None:
            positions = [p for p in positions if p['side'].lower() == sig.side]
        if not positions:
            return _skip('no_position', f'no open {sig.side + " " if sig.side else ""}{symbol} position'
                         + (' (paper)' if paper else ''))
        if len(positions) > 1:
            return _skip('ambiguous_position', f'{len(positions)} open {symbol} positions: send side')
        (position,) = positions
        if sig.action == 'close':
            return await self._close(position)
        if sig.action == 'reduce':
            res = await self.engine.execute_partial_exit(position, sig.size['close_pct'], reason='signal')
            if not res.get('success'):
                if 'is not open' in str(res.get('error')):
                    return {**res, 'status': 'skipped', 'reason': 'no_position'}
                # A size the venue cannot accept is the request's problem, not the venue's.
                return {**res, 'status': 'skipped' if 'sizing' in res else 'failed',
                        'reason': 'reduce_refused' if 'sizing' in res else 'reduce_failed'}
            return {**res, 'position_id': position['id']}
        # move_stop
        res = await self.engine.move_stop(position, sig.stop.price, reason='signal')
        if not res.get('success'):
            failed = res.get('code') in ('no_price', 'venue_rejected', 'not_recorded')
            return {**res, 'status': 'failed' if failed else 'skipped', 'reason': res.get('code')}
        return {**res, 'position_id': position['id']}

    async def _positions(self, symbol: str | None, paper: bool) -> list[dict[str, Any]]:
        return [p for p in await self.engine._own_open_positions(all_modes=True)
                if (symbol is None or p['symbol'] == symbol) and bool(p.get('dry_run')) == paper]

    async def _open(self, sig: EventSignal, symbol: str, paper: bool, row_id: int,
                    flip: bool = False) -> dict[str, Any]:
        # false = none for this position: an empty spec, which replaces the deployment's
        tp, tr, be = sig.take_profit, sig.trailing, sig.break_even
        result = await self.engine.execute_entry({
            'symbol': symbol,
            'direction': sig.side,
            'size': dict(sig.size),
            'stop_price': sig.stop.price if sig.stop else None,
            'stop_pct': sig.stop.pct if sig.stop else None,
            'take_profits': [] if tp is False else _ladder(tp) if tp else None,
            'trailing': {} if tr is False else tr.model_dump() if tr else None,
            'break_even': {} if be is False else be.model_dump(exclude_none=True) if be else None,
            'signal_id': row_id,
            'reason': f'signal:{sig.source}',
            'dry_run': paper,
            'flip': flip,
        })
        return {**result, 'action': sig.action}

    async def _close(self, position: dict[str, Any]) -> dict[str, Any]:
        try:
            status = await self.engine.execute_exit(position, reason='signal')
        except Exception as e:
            return {'success': False, 'status': 'failed', 'reason': 'close_failed',
                    'error': humanize_order_error(str(e)), 'position_id': position['id']}
        if status == 'in_progress':
            return _skip('close_in_progress', 'another request is already closing this position')
        return {'success': True, 'position_id': position['id'], 'close': status}

    async def _close_all(self, paper: bool) -> dict[str, Any]:
        # Entries already under way finish first, so the positions they open are closed too.
        in_flight = list(getattr(self.engine, '_entries_in_flight', ()))
        if in_flight:
            await asyncio.wait(in_flight)
        positions = await self._positions(None, paper)
        if not positions:
            return {'success': True, 'closed': 0, 'note': 'nothing open' + (' (paper)' if paper else '')}
        closed, errors = 0, []
        for p in positions:
            res = await self._close(p)
            if res['success']:
                closed += 1
            else:
                errors.append({'position_id': p['id'], 'symbol': p['symbol'],
                               'error': res.get('error') or res.get('reason')})
        out = {'success': not errors, 'closed': closed, 'errors': errors}
        return out if not errors else {**out, 'status': 'failed', 'reason': 'close_failed'}


async def signal_event(events, row_id: int, source: str, what: str, status: str, reasons: list | None) -> None:
    """A signal's outcome as an event (signal.executed / skipped / failed)."""
    if events is None:
        return
    why = f" - {'; '.join(str(r) for r in reasons)}" if reasons else ""
    await events.record(f"signal.{status}", "warning" if status == "failed" else "info",
                        f"{source}: {what} {status}{why}",
                        {"signal": row_id, "source": source, "status": status, "reasons": reasons})


def _ladder(levels) -> list[dict[str, Any]]:
    return [lv.model_dump(exclude_none=True) for lv in levels]


def _skip(reason: str, error: str) -> dict[str, Any]:
    return {'success': False, 'status': 'skipped', 'reason': reason, 'error': error}


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        return v if math.isfinite(v) else None       # JSON has no NaN / Infinity (Postgres refuses it)
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    return str(v)
