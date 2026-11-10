"""UTC timestamps that behave the same on SQLite and Postgres.

Postgres stores `timestamptz` natively. SQLite has no timestamp type, so timestamps are stored as
fixed-width UTC text (`YYYY-MM-DD HH:MM:SS.ffffff+00:00`). Fixed width keeps string comparison
in SQL (`WHERE closed_at >= ?`) chronological, and the same format is used whether a row is
written through the ORM or through raw SQL (see `relay.db.database`).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.types import TypeDecorator

_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?([+-]\d{2}:\d{2}|Z)?$")


def to_utc(value: datetime) -> datetime:
    """Aware UTC. Naive datetimes are taken to be UTC already (the relay never uses local time)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_utc(value: datetime) -> str:
    return to_utc(value).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def parse_utc(value: str) -> datetime | None:
    if not _ISO_TS.match(value):
        return None
    return to_utc(datetime.fromisoformat(value))


# Raw SQL on SQLite binds datetimes through the sqlite3 module; give it the same format the ORM
# type writes. (Also replaces sqlite3's default adapter, deprecated since Python 3.12.)
sqlite3.register_adapter(datetime, format_utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(32))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return format_utc(value)
        return to_utc(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            return parse_utc(value)
        return to_utc(value)
