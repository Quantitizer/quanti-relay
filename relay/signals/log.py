"""Signal log: every instruction the relay receives, its verdict and its result (table `signals`).

`record` is idempotent per (source, signal_id): a repeat returns the existing row with
`duplicate=True` and must not be executed again.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from relay.db.models import Signal

_INTERRUPTED = "interrupted: the relay stopped while carrying it out - check the position on the venue"


class SignalLog:
    def __init__(self, db):
        self.db = db

    async def record(self, source: str, signal_id: str, payload: dict, *, deployment: str | None = None,
                     kind: str | None = None, symbol: str | None = None,
                     status: str = "accepted") -> tuple[Signal, bool]:
        """Insert a signal row. Returns (row, duplicate)."""
        async with self.db.session() as s:
            row = Signal(source=source, signal_id=signal_id, payload=payload, deployment=deployment,
                         kind=kind, symbol=symbol, status=status)
            s.add(row)
            try:
                await s.commit()
                return row, False
            except IntegrityError:
                await s.rollback()
        existing = await self.get_by_source_id(source, signal_id)
        if existing is None:
            # Not the (source, signal_id) uniqueness: a different integrity problem - surface it.
            raise RuntimeError(f"signal ({source}, {signal_id}) could not be recorded")
        return existing, True

    async def recent(self, limit: int = 100, status: str | None = None) -> list[Signal]:
        """The latest signals, newest first (optionally only those with this status)."""
        q = select(Signal)
        if status:
            q = q.where(Signal.status == status)
        async with self.db.session() as s:
            return list((await s.execute(q.order_by(Signal.id.desc()).limit(limit))).scalars().all())

    async def get(self, row_id: int) -> Signal | None:
        async with self.db.session() as s:
            return await s.get(Signal, row_id)

    async def get_by_source_id(self, source: str, signal_id: str) -> Signal | None:
        async with self.db.session() as s:
            return (await s.execute(select(Signal).where(
                Signal.source == source, Signal.signal_id == signal_id))).scalar_one_or_none()

    async def _set(self, row_id: int, status: str, reasons: list | None = None, result: Any = None) -> None:
        async with self.db.session() as s:
            row = await s.get(Signal, row_id)
            if row is None:
                logger.warning(f"[SignalLog] signal {row_id} not found - cannot record status {status}")
                return
            row.status = status
            if reasons is not None:
                row.reasons = reasons
            if result is not None:
                row.result = result
            row.updated_at = datetime.now(UTC)
            await s.commit()

    async def fail_unfinished(self, deployment: str) -> int:
        """At start: signals of `deployment` still 'accepted' were being carried out when the relay
        stopped (nothing carries them out any more). Marked failed, so none looks pending forever.
        Returns how many."""
        async with self.db.session() as s:
            rows = (await s.execute(select(Signal).where(Signal.status == "accepted",
                                                         Signal.deployment == deployment))).scalars().all()
            for row in rows:
                row.status, row.reasons = "failed", [_INTERRUPTED]
                row.updated_at = datetime.now(UTC)
            await s.commit()
        if rows:
            logger.warning(f"[SignalLog] {len(rows)} signal(s) were cut off by a restart - marked failed")
        return len(rows)

    async def mark_result(self, row_id: int, status: str, reasons: list | None = None, result: Any = None) -> None:
        """The outcome of a signal executed outside the entry path (close, reduce, move_stop ...)."""
        await self._set(row_id, status, reasons=reasons, result=result)

    # Engine interface -------------------------------------------------------------------
    async def mark_skipped(self, row_id: int, reason: str) -> None:
        await self._set(row_id, "skipped", reasons=[reason])

    async def mark_executed(self, row_id: int) -> None:
        await self._set(row_id, "executed")

    async def mark_outcome(self, row_id: int, outcome: str) -> None:
        """An executed signal whose position did not stand as intended (e.g. the stop could not be
        placed and the position was closed again): status stays 'executed' (an order did fill), the
        outcome is recorded as its reason."""
        await self._set(row_id, "executed", reasons=[outcome])
