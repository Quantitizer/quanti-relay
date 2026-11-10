"""Copies of the SQLite database: one is made before every schema upgrade, and `relay db backup`
makes one on demand.

A copy goes to `backups/` next to the database file, named after the schema it holds and the time
(`relay-<revision>-<YYYYmmddTHHMMSSZ>.db`); the newest BACKUPS_KEEP are kept, and the oldest copy of
each revision as well: the one made before an upgrade stays even when a failing start makes copies
again and again. The copy is made with
SQLite's own backup, so it is consistent even while the relay writes. To restore one: stop the
relay, copy it over the database file, start the relay.

Postgres is backed up with its own tools (pg_dump); the relay only reminds you before an upgrade.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

BACKUPS_KEEP = 5
_NAME = re.compile(r"^relay-[0-9a-z_]+-\d{8}T\d{6}Z\.db$")


def sqlite_path(url: str) -> Path | None:
    """The database file of a SQLite URL (None: another database, or one held in memory)."""
    u = make_url(url)
    if not u.drivername.startswith("sqlite") or not u.database or u.database == ":memory:":
        return None
    return Path(u.database)


def current_revision(path: Path) -> str | None:
    """The schema revision the file is at (None: a new or empty database)."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        return None                                   # no schema yet
    finally:
        con.close()
    return row[0] if row else None


def backup(path: Path, revision: str | None = None, now: datetime | None = None) -> Path:
    """Copy the database to backups/ beside it, then keep only the newest BACKUPS_KEEP copies."""
    folder = path.parent / "backups"
    folder.mkdir(exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    rev = re.sub(r"[^0-9a-z_]", "_", (revision or current_revision(path) or "none").lower())
    target = folder / f"relay-{rev}-{stamp}.db"
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    copies = sorted((p for p in folder.iterdir() if _NAME.match(p.name)), key=lambda p: p.name[-19:])
    first_of_revision = {}
    for c in copies:
        first_of_revision.setdefault(c.name[len("relay-"):-len("-YYYYmmddTHHMMSSZ.db")], c)
    for old in copies[:-BACKUPS_KEEP]:
        if old not in first_of_revision.values():
            old.unlink()
    return target
