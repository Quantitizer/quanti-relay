"""Event log: what the relay tells the user about, kept in the database (table `events`).

Each event is also logged (warning / critical ones at that level). Nothing is lost while nobody is
watching: GET /v1/events lists them, and the notification sinks (R4: Telegram, Discord,
webhook-out) deliver the ones not delivered yet.

Kinds (severity):
  relay.started               (info)      the relay started: deployment, paper or LIVE, venue host
  relay.halted                (critical)  the emergency stop: entries stopped, positions being closed
  relay.resumed               (warning)   the owner lifted the emergency stop
  relay.stuck                 (critical)  a background loop ended or hangs: positions and stops may not be watched
  config.changed              (warning; critical when set to live) the console changed a config file
  venue.checked               (info / warning) a venue check from the console passed / failed
  auth.locked                 (warning)   an address sent too many wrong API keys and is refused for a while
  signal.executed / signal.skipped (info), signal.failed (warning), signal.rejected (info)
                                          a signal's outcome, with its reasons
  signal.conflict             (warning)   a signal id reused with a different body: refused, not carried out
  position.opened             (info)      a position of the deployment opened (any path)
  position.closed             (info)      ... closed (any path: the relay, its stop, the venue), with PnL
  position.stop_failed        (critical when it may be open without a stop, else warning)
  position.drift_detected     (warning)   a live position's size differs from the venue's (seen once)
  position.drift_repaired     (warning)   ... still the same on the next sync: the record was repaired
  position.drift_unrepairable (critical)  ... but the venue position is shared with other records
  position.naked              (critical)  open on the venue without a stop, and the stop audit could not set one
  position.unwatched          (critical)  live positions are recorded, but there are no exchange keys to watch them

Positions are watched in the database (watch_positions), not hooked into each close path, so a
close the venue made (a stop that fired) is reported like one the relay made.
"""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import exists, select

from relay.db.models import Cursor, Event, Position

# Positions reported at most this far back (a restart after a long stop does not replay history).
WATCH_WINDOW = timedelta(days=1)

SEVERITIES = ("info", "warning", "critical")


# Warning and critical events that could not be stored, kept in memory (the oldest dropped first).
PENDING_MAX = 500


class EventLog:
    def __init__(self, db, deployment_id: int | None = None):
        self.db = db
        self.deployment_id = deployment_id
        # Events the database refused (it is down, the disk is full). They are stored when it is back,
        # and meanwhile the notifier sends them straight to the sinks: a critical alert must not wait
        # for the database it may be about.
        self.pending: deque[dict[str, Any]] = deque(maxlen=PENDING_MAX)

    async def record(self, kind: str, severity: str, message: str, data: dict[str, Any] | None = None,
                     position_id: int | None = None) -> int | None:
        """Store (and log) an event. Never raises: an event that cannot be stored is still logged."""
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        getattr(logger, "info" if severity == "info" else severity)(f"[Event] {kind}: {message}")
        try:
            async with self.db.session() as s:
                row = Event(kind=kind, severity=severity, message=message, data=data,
                            deployment_id=self.deployment_id, position_id=position_id,
                            created_at=datetime.now(UTC))
                s.add(row)
                await s.commit()
                return row.id
        except Exception as e:
            logger.error(f"[Event] {kind} could not be stored ({e}): {message}")
            if severity != "info":
                self.pending.append({"kind": kind, "severity": severity, "message": message, "data": data,
                                     "position_id": position_id, "created_at": datetime.now(UTC), "sent_to": []})
            return None

    async def store_pending(self) -> None:
        """Store the events the database refused earlier, oldest first, and stop at the first that still
        fails. The sinks they already reached are noted in the event (data._sent_to), so they are not
        sent twice."""
        while self.pending:
            p = self.pending[0]
            data = dict(p["data"] or {})
            if p["sent_to"]:
                data["_sent_to"] = list(p["sent_to"])
            try:
                async with self.db.session() as s:
                    s.add(Event(kind=p["kind"], severity=p["severity"], message=p["message"], data=data or None,
                                deployment_id=self.deployment_id, position_id=p["position_id"],
                                created_at=p["created_at"]))
                    await s.commit()
            except Exception:
                return
            self.pending.popleft()

    async def recent(self, limit: int = 100, kind: str | None = None) -> list[Event]:
        async with self.db.session() as s:
            q = select(Event).order_by(Event.id.desc()).limit(limit)
            if kind:
                q = q.where(Event.kind == kind)
            return list((await s.execute(q)).scalars().all())

    # Cursors (how far a named reader has got) -------------------------------------------------
    async def cursor(self, name: str) -> int | None:
        async with self.db.session() as s:
            row = await s.get(Cursor, name)
            return None if row is None else int(row.value)

    async def set_cursor(self, name: str, value: int) -> None:
        async with self.db.session() as s:
            row = await s.get(Cursor, name)
            if row is None:
                s.add(Cursor(name=name, value=value, updated_at=datetime.now(UTC)))
            else:
                row.value, row.updated_at = value, datetime.now(UTC)
            await s.commit()

    # Position watcher --------------------------------------------------------------------------
    async def watch_positions(self) -> int:
        """Report the deployment's positions opened or closed since the watcher started (at most
        WATCH_WINDOW back), each once: position.opened / position.closed. Returns how many."""
        since_ms = await self.cursor("watch:since")
        if since_ms is None:                         # first run ever: from now, not the history
            since_ms = int(datetime.now(UTC).timestamp() * 1000)
            await self.set_cursor("watch:since", since_ms)
        since = max(datetime.fromtimestamp(since_ms / 1000, UTC), datetime.now(UTC) - WATCH_WINDOW)
        reported = 0
        for kind, when in (("position.opened", Position.opened_at), ("position.closed", Position.closed_at)):
            async with self.db.session() as s:
                q = select(Position).where(
                    when.isnot(None), when >= since,
                    ~exists().where(Event.position_id == Position.id, Event.kind == kind))
                if kind == "position.closed":
                    q = q.where(Position.status == "CLOSED")
                if self.deployment_id is not None:
                    q = q.where(Position.deployment_id == self.deployment_id)
                rows = (await s.execute(q.order_by(when.asc()).limit(200))).scalars().all()
            for p in rows:
                await self.record(kind, "info", _describe(kind, p), _facts(p), p.id)
                reported += 1
        return reported


def _num(v) -> str:
    return "?" if v is None else f"{float(v):.8g}"


def _describe(kind: str, p: Position) -> str:
    side = (p.side or "").lower()
    mode = " (paper)" if p.dry_run else ""
    if kind == "position.opened":
        return (f"{p.symbol} {side} {_num(p.size)} @ {_num(p.entry_price)}{mode} opened"
                + (f", stop {_num(p.initial_stop_price)}" if p.initial_stop_price else ", NO STOP"))
    pnl = (f"PnL {float(p.pnl):+.2f}" + (f" ({float(p.pnl_percent):+.2f}%)" if p.pnl_percent is not None else "")
           if p.pnl is not None else "PnL pending (from the venue's records)")
    return f"{p.symbol} {side}{mode} closed @ {_num(p.exit_price)}: {pnl}"


def _facts(p: Position) -> dict[str, Any]:
    return {"symbol": p.symbol, "side": (p.side or "").lower(), "size": p.size, "entry_price": p.entry_price,
            "exit_price": p.exit_price, "stop_price": p.stop_price, "pnl": p.pnl, "pnl_percent": p.pnl_percent,
            "pnl_source": p.pnl_source, "paper": bool(p.dry_run)}
