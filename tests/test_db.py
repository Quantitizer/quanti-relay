from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from relay.db.models import Account, Deployment, Position

_INSERT = (
    "INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, opened_at,"
    " created_at, updated_at) VALUES ($1, $2, $3, 1, 100, 'OPEN', $4, $4, $4)"
    " ON CONFLICT (coalesce(deployment_id, -1), symbol, side, dry_run) WHERE status = 'OPEN' DO NOTHING"
    " RETURNING id"
)


async def _deployment(db, name="d1"):
    async with db.session() as s:
        acc = Account(name=f"acc-{name}")
        s.add(acc)
        await s.flush()
        dep = Deployment(name=name, account_id=acc.id)
        s.add(dep)
        await s.commit()
        return dep.id


async def test_one_open_row_per_deployment_symbol_side(db):
    now = datetime.now(UTC)
    d1, d2 = await _deployment(db, "d1"), await _deployment(db, "d2")
    async with db.acquire() as c:
        assert await c.fetchrow(_INSERT, d1, "BTCUSDT", "LONG", now) is not None
        assert await c.fetchrow(_INSERT, d1, "BTCUSDT", "LONG", now) is None      # duplicate
        assert await c.fetchrow(_INSERT, d2, "BTCUSDT", "LONG", now) is not None  # other deployment
        assert await c.fetchrow(_INSERT, None, "BTCUSDT", "LONG", now) is not None
        assert await c.fetchrow(_INSERT, None, "BTCUSDT", "LONG", now) is None    # NULLs collide too
        await c.execute("UPDATE positions SET status = 'CLOSED' WHERE deployment_id = $1", d1)
        assert await c.fetchrow(_INSERT, d1, "BTCUSDT", "LONG", now) is not None  # closed frees it


async def test_raw_and_orm_timestamps_agree(db):
    t = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    async with db.acquire() as c:
        await c.fetchrow(_INSERT, None, "ETHUSDT", "SHORT", t)
        row = await c.fetchrow("SELECT opened_at FROM positions WHERE symbol = $1", "ETHUSDT")
        assert row["opened_at"] == t and row["opened_at"].tzinfo is not None
        # chronological comparison against a bound parameter works on every backend
        assert await c.fetchval("SELECT count(*) FROM positions WHERE opened_at >= $1", t) == 1
        assert await c.fetchval(
            "SELECT count(*) FROM positions WHERE opened_at > $1", t + timedelta(microseconds=1)) == 0
    async with db.session() as s:
        p = (await s.execute(select(Position).where(Position.symbol == "ETHUSDT"))).scalar_one()
        assert p.opened_at == t
        p.closed_at = t + timedelta(hours=1)
        await s.commit()
    async with db.acquire() as c:
        assert await c.fetchval("SELECT closed_at FROM positions") == t + timedelta(hours=1)


async def test_naive_datetimes_are_treated_as_utc(db):
    async with db.acquire() as c:
        await c.fetchrow(_INSERT, None, "SOLUSDT", "LONG", datetime(2026, 5, 1, 12, 0))
        got = await c.fetchval("SELECT opened_at FROM positions")
    assert got == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


async def test_execute_status_and_record_access(db):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        assert await c.execute("UPDATE positions SET size = 2") == "UPDATE 0"
        await c.fetchrow(_INSERT, None, "BTCUSDT", "LONG", now)
        assert await c.execute("UPDATE positions SET size = $1 WHERE symbol = $2", 2.5, "BTCUSDT") == "UPDATE 1"
        row = await c.fetchrow("SELECT symbol, size FROM positions")
        assert row[0] == row["symbol"] == "BTCUSDT" and row[1] == 2.5
        assert dict(row) == {"symbol": "BTCUSDT", "size": 2.5}
        # a repeated placeholder binds the same value
        assert await c.fetchval("SELECT count(*) FROM positions WHERE symbol = $1 OR symbol = $1", "BTCUSDT") == 1


async def test_transaction_rolls_back_and_autocommit_otherwise(db):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        pid = (await c.fetchrow(_INSERT, None, "BTCUSDT", "LONG", now))["id"]
        with pytest.raises(RuntimeError):
            async with c.transaction():
                await c.execute("UPDATE positions SET size = 9 WHERE id = $1", pid)
                raise RuntimeError("boom")
        assert await c.fetchval("SELECT size FROM positions WHERE id = $1", pid) == 1
        async with c.transaction():
            await c.execute("UPDATE positions SET size = 3 WHERE id = $1", pid)
    async with db.acquire() as other:  # committed: visible from a different connection
        assert await other.fetchval("SELECT size FROM positions WHERE id = $1", pid) == 3


async def test_failed_statement_does_not_poison_the_connection(db):
    async with db.acquire() as c:
        with pytest.raises(IntegrityError):
            await c.execute("INSERT INTO positions (symbol) VALUES ($1)", "X")  # NOT NULL violation
        assert await c.fetchval("SELECT count(*) FROM positions") == 0


def test_baseline_migration_matches_models(tmp_path):
    """One fresh baseline migration; the models must not drift from it."""
    import io

    from alembic import command
    from alembic.config import Config

    # Explicit stream: Alembic's default binds sys.stdout at import time, which under pytest may be
    # an earlier test's (closed) capture stream.
    cfg = Config("alembic.ini", stdout=io.StringIO())
    cfg.attributes["url"] = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if autogenerate would emit anything
    # Autogenerate cannot see the partial unique index on open positions; a batch-mode migration on
    # SQLite would drop it silently (the table is rebuilt without it).
    import sqlite3
    with sqlite3.connect(tmp_path / "m.db") as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND "
                         "name = 'uq_open_position_deployment_symbol_side'").fetchone()
    command.downgrade(cfg, "base")


async def test_text_that_looks_like_a_date_stays_text(db):
    """Only timestamp columns become datetimes: an order id or reason that resembles a date does not."""
    now = datetime.now(UTC)
    async with db.acquire() as c:
        await c.fetchrow(_INSERT, None, "BTCUSDT", "LONG", now)
        await c.execute("UPDATE positions SET close_order_id = $1", "2026-10-30 12:00:00")
        row = await c.fetchrow("SELECT close_order_id, opened_at FROM positions")
    assert row["close_order_id"] == "2026-10-30 12:00:00" and isinstance(row["opened_at"], datetime)
