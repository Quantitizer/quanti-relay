"""Target executor: a v1 target signal (a whole book of weights) carried out on the engine.

  1. The signal's mode: paper when the deployment is in dry-run or the signal says dry_run: true;
     only positions of that mode are its book (as with event signals).
  2. The book must be settled and true: no close of it in progress, one position per symbol, and
     (live) every size exactly what the venue holds. Otherwise nothing is traded: a book diffed
     from a wrong picture would trade the wrong amounts.
  3. Plan (relay.target_book): weights x equity x gross_leverage, the deployment's rails, its
     neutralize option, the venue's minimums, then enter / exit / resize / flip / hold.
  4. Execute, margin first: exits, reductions, flips, increases, entries - each through the engine
     with all of its checks (an entry or increase can still be refused by them: reported per leg).
     If a close or reduction fails, the increases and entries are NOT attempted (the margin they
     need was not freed).

Each new leg carries the signal's stop_pct as its stop and nothing else: the deployment's default
take-profit, trailing stop and break-even are not applied to a book that is rebalanced as a whole.
An existing position keeps its stop when it is resized. One target signal runs at a time.

The outcome is recorded on the signal row: executed (all legs done or held), failed (a leg could
not be done; the reasons say which), or skipped (nothing traded: the reasons say why).
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from relay.signals.contract import TargetSignal, unified_symbol
from relay.signals.executor import _jsonable, bybit_symbol, signal_event
from relay.target_book import (
    Leg,
    apply_rails,
    drop_below_minimum,
    execution_order,
    plan_actions,
    target_notionals,
)

_SIZE_TOL = 1e-9


class TargetExecutor:
    def __init__(self, engine, signal_log, events=None):
        self.engine = engine
        self.signal_log = signal_log
        self.events = events
        self._lock = asyncio.Lock()

    async def execute(self, sig: TargetSignal, row_id: int) -> dict[str, Any]:
        """Carry out `sig` (authenticated, validated, logged as row `row_id`). Never raises (a
        cancel at shutdown is recorded, then passed on)."""
        try:
            async with self._lock:
                result = await self._execute(sig)
        except asyncio.CancelledError:
            await self._record(row_id, {'success': False, 'status': 'failed', 'reason': 'interrupted',
                                        'error': 'cut off at shutdown - check the book on the venue'}, sig)
            raise
        except Exception as e:
            logger.opt(exception=True).error(f"[Target] #{row_id}: {e}")
            result = {'success': False, 'status': 'failed', 'reason': 'internal_error', 'error': str(e)}
        await self._record(row_id, result, sig)
        return result

    async def _record(self, row_id: int, result: dict[str, Any], sig: TargetSignal | None = None) -> None:
        status = result.get('status') or ('executed' if result.get('success') else 'skipped')
        reasons = result.get('reasons') or ([result['reason']] if result.get('reason') else None)
        try:
            await self.signal_log.mark_result(row_id, status, reasons=reasons, result=_jsonable(result))
        except Exception as e:
            logger.error(f"[Target] #{row_id}: outcome {status} could not be recorded: {e}")
        if sig is not None:
            await signal_event(self.events, row_id, sig.source, f"target book ({len(sig.weights)} symbols)",
                               status, reasons)

    async def _execute(self, sig: TargetSignal) -> dict[str, Any]:
        eng = self.engine
        paper = await eng.get_dry_run_mode() or sig.dry_run is True
        if sig.execution is not None and sig.execution.type != 'market':
            return _skip('execution_not_supported',
                         f'execution {sig.execution.type} is not supported yet - send market (or omit it)')
        try:
            venue = {u: bybit_symbol(u) for u in sig.weights}
        except ValueError as e:
            return _skip('unsupported_symbol', str(e))
        settings = await eng._get_execution_settings()
        options = settings.get('target') or {}
        if settings.get('require_stop', True) and sig.stop_pct is None:
            # Every new leg would be refused for want of a stop - after its old side was closed.
            return _skip('stop_required', 'the deployment requires a stop: send stop_pct with the book')
        if sig.stop_pct is not None and not getattr(eng.bybit, 'can_stop', True):
            return _skip('stop_unsupported', 'this venue cannot hold a stop order: send the book without stop_pct')
        if any(w < 0 for w in sig.weights.values()) and not getattr(eng.bybit, 'can_short', True):
            return _skip('short_unsupported', 'this venue is long only (spot): a book with short legs is refused')

        # 2. The book: this deployment's positions of the signal's mode, settled and true.
        book: dict[str, dict] = {}
        for p in await eng._own_open_positions(all_modes=True):
            if bool(p.get('dry_run')) != paper:
                continue
            if p.get('status') != 'OPEN' or eng._close_owned(p['id']):
                return _skip('book_unsettled', f"{p['symbol']}: a close of position {p['id']} is in progress")
            try:
                u = unified_symbol(p['symbol'])
            except ValueError:
                return _skip('book_unsettled', f"{p['symbol']}: position {p['id']} has a symbol the book cannot name")
            if u in book:
                return _skip('book_unsettled', f"{p['symbol']}: more than one open position")
            book[u] = p
            venue.setdefault(u, p['symbol'])
        if not paper:
            problem = await self._venue_mismatch(book, venue)
            if problem:
                return _skip(*problem)

        # 3. Plan.
        try:
            equity, _ = await eng.bybit.get_balance_safe(coin="USDT")
        except Exception as e:
            return _skip('no_equity', f'account equity unreadable ({e})')
        if not equity or equity <= 0:
            return _skip('no_equity', 'account equity unavailable')
        prices: dict[str, float] = {}
        specs: dict[str, dict] = {}
        for u, s in venue.items():
            try:
                t = await eng.bybit.get_ticker(symbol=s, category='linear')
                px = float(t.get('markPrice') or t.get('lastPrice') or 0)
                spec = await eng._instrument_specs(s)
            except Exception as e:
                return _skip('no_price', f'{s}: price or lot rules unreadable ({e})')
            if px <= 0 or not spec:
                return _skip('no_price', f'{s}: no price or lot rules')
            prices[u], specs[u] = px, spec
        target = target_notionals(sig.weights, equity, sig.gross_leverage)
        target, notes = apply_rails(target, settings.get('rails'), equity)
        target, dropped = drop_below_minimum(target, prices, specs, bool(options.get('neutralize')))
        notes += dropped
        current = {u: Leg(p['side'].lower(), float(p['size']), float(p['size']) * prices[u]) for u, p in book.items()}
        actions = plan_actions(target, current, equity, options.get('min_change_pct'), prices, specs)

        # 4. Execute, margin first.
        legs: list[dict[str, Any]] = []
        freed = True
        for a in execution_order(actions):
            closing = a.action in ('exit', 'flip') or (a.action == 'resize' and a.target_notional < a.current_notional)
            if not closing and not freed:
                legs.append({'symbol': a.symbol, 'action': a.action, 'success': False, 'reason': 'not_attempted',
                             'error': 'an earlier close or reduction failed: no new exposure'})
                continue
            if a.action == 'flip' and not freed:
                # Its close half still goes (it frees margin); its new side would be new exposure.
                closed = await self._close(book.get(a.symbol))
                legs.append({'symbol': a.symbol, 'action': a.action, **closed, 'success': False,
                             'reason': 'close_failed' if not closed['success'] else 'not_attempted',
                             'error': closed.get('error') or 'closed only: an earlier close or reduction failed'})
                continue
            leg = await self._leg(a, book.get(a.symbol), venue[a.symbol], prices[a.symbol], sig, paper)
            legs.append(leg)
            if closing and not leg['success'] and leg.get('closed') is not True:
                freed = False
        held = [a.symbol for a in actions if a.action == 'hold']
        failed = [lg for lg in legs if not lg['success']]
        out = {'success': not failed, 'dry_run': paper, 'equity': equity, 'legs': legs, 'held': held,
               'notes': notes, 'target': target}
        if failed:
            out.update(status='failed', reasons=[f"{lg['symbol']} {lg['action']}: {lg.get('reason')}" for lg in failed])
        logger.info(f"[Target] {'paper ' if paper else ''}book: {len(legs)} leg(s), {len(failed)} failed, "
                    f"{len(held)} held")
        return out

    async def _venue_mismatch(self, book: dict[str, dict], venue: dict[str, str]) -> tuple[str, str] | None:
        """(reason, why) when the live picture is not this book's alone, else None. Every symbol
        of the book AND of the target is checked: the venue must hold exactly the book's position
        (none where the book has none, so an entry cannot net into a position the relay does not
        know), and no other live record may share the symbol (another deployment's, or one
        imported from outside the relay)."""
        async with self.engine.db.acquire() as conn:
            for u, symbol in venue.items():
                mine = book.get(u)
                others = await conn.fetchval(
                    "SELECT count(*) FROM positions WHERE symbol = $1 AND status IN ('OPEN', 'CLOSING') "
                    "AND NOT dry_run AND id <> $2", symbol, mine['id'] if mine else -1)
                if others:
                    return ('shared_venue_position',
                            (f"{symbol}: {others} other live record(s) on this symbol (another deployment's, or "
                            f"imported) - one venue position cannot be traded as this book's"))
        for u, symbol in venue.items():
            mine = book.get(u)
            try:
                rows = await self.engine.bybit.get_positions(category='linear', symbol=symbol)
            except Exception as e:
                return 'book_out_of_sync', f"{symbol}: venue position unreadable ({e})"
            held = {side: sum(float(r.get('size') or 0) for r in rows if r.get('side') == side)
                    for side in ('Buy', 'Sell')}
            want = None if mine is None else ('Buy' if mine['side'].upper() == 'LONG' else 'Sell')
            expected = {'Buy': 0.0, 'Sell': 0.0}
            if mine is not None:
                expected[want] = float(mine['size'])
            for side, size in held.items():
                if abs(size - expected[side]) > max(_SIZE_TOL, expected[side] * 1e-6):
                    return ('book_out_of_sync',
                            (f"{symbol}: the book holds {expected[side]} on the {side.lower()} side, the venue {size}"
                            f" - the book must match the venue before it is traded (check the position)"))
        return None

    async def _leg(self, a, position, symbol: str, price: float, sig: TargetSignal, paper: bool) -> dict[str, Any]:
        eng = self.engine
        base = {'symbol': a.symbol, 'action': a.action, 'target': a.target_notional, 'current': a.current_notional}
        try:
            if a.action == 'exit':
                return {**base, **await self._close(position)}
            if a.action == 'resize' and a.target_notional < a.current_notional:
                pct = (a.current_notional - a.target_notional) / a.current_notional * 100
                res = await eng.execute_partial_exit(position, min(pct, 100.0), reason='target')
                return {**base, 'success': bool(res.get('success')), 'reason': None if res.get('success')
                        else res.get('error'), 'closed_qty': res.get('partial_qty')}
            if a.action == 'resize':
                res = await eng.execute_increase(position, (a.target_notional - a.current_notional) / price,
                                                 reason='target')
                return {**base, 'success': bool(res.get('success')), 'reason': res.get('reason'),
                        'added_qty': res.get('added_quantity')}
            flip = False
            if a.action == 'flip':
                closed = await self._close(position)
                if not closed['success']:
                    return {**base, **closed}
                flip = closed.get('closed') is True
            res = await eng.execute_entry({
                'symbol': symbol, 'direction': a.side, 'size': {'qty': a.target_notional / price},
                'entry_price': price,                     # the price the plan used, for the engine's checks too
                'stop_pct': sig.stop_pct, 'take_profits': [], 'trailing': {}, 'break_even': {},
                'reason': f'target:{sig.source}', 'dry_run': paper, 'flip': flip,
            })
            leg = {**base, 'success': bool(res.get('success')), 'reason': res.get('reason'),
                   'position_id': res.get('position_id')}
            if a.action == 'flip':
                leg['closed'] = True                      # the old side is closed either way
            return leg
        except Exception as e:
            logger.opt(exception=True).error(f"[Target] {a.symbol} {a.action}: {e}")
            return {**base, 'success': False, 'reason': 'internal_error', 'error': str(e)}

    async def _close(self, position) -> dict[str, Any]:
        try:
            status = await self.engine.execute_exit(position, reason='target')
        except Exception as e:
            return {'success': False, 'reason': 'close_failed', 'error': str(e)}
        if status == 'in_progress':
            return {'success': False, 'reason': 'close_in_progress'}
        return {'success': True, 'closed': True, 'close': status}


def _skip(reason: str, error: str) -> dict[str, Any]:
    return {'success': False, 'status': 'skipped', 'reason': reason, 'error': error}
