"""Database handle: one SQLAlchemy async engine, used two ways.

* `db.session()` — an ORM `AsyncSession`, for new code.
* `db.acquire()` — a connection with the asyncpg-style API (`fetch`, `fetchrow`, `fetchval`,
  `execute`, `transaction()`, `$1` placeholders) that the carried-over execution code is written
  against. It runs on SQLite and Postgres alike, so that code keeps its proven SQL unchanged
  apart from dialect-specific constructs, which are not allowed: no `::casts`, `ANY($n)`,
  `NOW()`, `INTERVAL`, JSON operators or `FOR UPDATE`. Expand lists in Python, pass times as
  parameters.

Like asyncpg, every statement outside `transaction()` commits on its own.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from relay.db.types import parse_utc, to_utc

_PLACEHOLDER = re.compile(r"\$(\d+)")


class Record(dict):
    """A result row: `row["col"]` like a dict, and `row[0]` by position like an asyncpg Record."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _translate(query: str, args: tuple) -> tuple[str, dict[str, Any]]:
    # Datetimes are bound as aware UTC: a naive one would otherwise be read in the database
    # session's time zone on Postgres (the relay never uses local time; see relay.db.types).
    params = {f"p{i}": (to_utc(v) if isinstance(v, datetime) else v) for i, v in enumerate(args, start=1)}
    return _PLACEHOLDER.sub(lambda m: f":p{m.group(1)}", query), params



_TS_COLUMNS: frozenset | None = None


def _timestamp_columns() -> frozenset:
    """Names of every timestamp column in the schema (relay.db.models), read once."""
    global _TS_COLUMNS
    if _TS_COLUMNS is None:
        from relay.db.models import Base
        from relay.db.types import UTCDateTime
        _TS_COLUMNS = frozenset(c.name for t in Base.metadata.tables.values() for c in t.columns
                                if isinstance(c.type, UTCDateTime))
    return _TS_COLUMNS

class Connection:
    def __init__(self, conn: AsyncConnection, sqlite: bool):
        self._conn = conn
        self._sqlite = sqlite
        self._tx_depth = 0

    def _row(self, row) -> Record:
        rec = Record(row._mapping)
        if self._sqlite:
            # SQLite returns timestamps as text; hand back aware datetimes as Postgres does - for the
            # schema's timestamp columns only (a text value that merely looks like a date stays text).
            ts_cols = _timestamp_columns()
            for k, v in rec.items():
                if k in ts_cols and isinstance(v, str):
                    parsed = parse_utc(v)
                    if parsed is not None:
                        rec[k] = parsed
        return rec

    async def _run(self, query: str, args: tuple, consume):
        sql, params = _translate(query, args)
        try:
            result = await self._conn.execute(text(sql), params)
            out = consume(result)
        except Exception:
            if self._tx_depth == 0:
                await self._conn.rollback()
            raise
        if self._tx_depth == 0:
            await self._conn.commit()
        return out

    async def fetch(self, query: str, *args) -> list[Record]:
        return await self._run(query, args, lambda r: [self._row(x) for x in r] if r.returns_rows else [])

    async def fetchrow(self, query: str, *args) -> Record | None:
        def first(r):
            if not r.returns_rows:
                return None
            row = r.first()
            return self._row(row) if row is not None else None
        return await self._run(query, args, first)

    async def fetchval(self, query: str, *args, column: int = 0):
        row = await self.fetchrow(query, *args)
        return None if row is None else row[column]

    async def execute(self, query: str, *args) -> str:
        """Returns an asyncpg-style status string, e.g. 'UPDATE 3'."""
        def status(r):
            verb = (query.lstrip().split(None, 1) or ["SELECT"])[0].upper()
            n = r.rowcount if r.rowcount is not None and r.rowcount >= 0 else 0
            return f"INSERT 0 {n}" if verb == "INSERT" else f"{verb} {n}"
        return await self._run(query, args, status)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        if self._tx_depth:
            async with self._conn.begin_nested():
                self._tx_depth += 1
                try:
                    yield
                finally:
                    self._tx_depth -= 1
            return
        if self._conn.in_transaction():
            await self._conn.commit()
        self._tx_depth = 1
        try:
            yield
        except BaseException:
            self._tx_depth = 0
            await self._conn.rollback()
            raise
        self._tx_depth = 0
        await self._conn.commit()


class Database:
    def __init__(self, url: str, echo: bool = False):
        self.url = url
        self.sqlite = url.startswith("sqlite")
        kwargs: dict[str, Any] = {"echo": echo}
        if self.sqlite:
            kwargs["connect_args"] = {"timeout": 30}
        self.engine = create_async_engine(url, **kwargs)
        if self.sqlite:
            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Connection]:
        async with self.engine.connect() as conn:
            yield Connection(conn, self.sqlite)

    def session(self) -> AsyncSession:
        return self._sessions()

    async def create_all(self) -> None:
        """Create every table directly from the models (tests). The relay itself creates and upgrades
        its schema with Alembic (relay db upgrade / startup), so a database made here is for tests only."""
        from relay.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
