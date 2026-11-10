"""Keyed account book importer — the orphan cure.

On an account with its own keys nothing else can CREATE a DB row, so a missed record would be a
permanent unmanaged position. These tests pin that adoption happens, that it refuses to guess an
owner, and that it never writes when the exchange view is incomplete. They run on the real database.
"""
from datetime import UTC, datetime

from sqlalchemy import select

from relay.db.models import Account, Deployment, Position
from relay.execution.keyed_book_importer import import_keyed_account_positions


class _Adapter:
    def __init__(self, positions=None, raises=False):
        self._p = positions or []
        self._raises = raises

    async def get_positions(self, **kw):
        if self._raises:
            raise RuntimeError("circuit breaker open")
        return self._p


def _pos(symbol, side, size, avg=100.0, created=None):
    return {"symbol": symbol, "side": side, "size": str(size), "avgPrice": str(avg),
            "unrealisedPnl": "0", "createdTime": str(created or 1754000000000)}


async def _account(db, *deployments_enabled):
    """A keyed account with one deployment per flag; returns their ids."""
    async with db.session() as s:
        acc = Account(name="sub", credentials_ref="SUB_ACCOUNT_1")
        s.add(acc)
        await s.flush()
        deps = [Deployment(name=f"d{i}", account_id=acc.id, enabled=e) for i, e in enumerate(deployments_enabled)]
        s.add_all(deps)
        await s.commit()
        return [d.id for d in deps]


async def _rows(db):
    async with db.session() as s:
        return list((await s.execute(select(Position).order_by(Position.id))).scalars().all())


async def _add(db, **kw):
    kw.setdefault("status", "OPEN")
    kw.setdefault("opened_at", datetime.now(UTC))
    async with db.session() as s:
        s.add(Position(**kw))
        await s.commit()


async def test_adopts_untracked_exchange_position(db):
    """THE CURE. A position on the exchange with no OPEN DB row is unmanaged — never exited, never
    risk-accounted, but real exposure. It must be adopted into the owning deployment."""
    (dep,) = await _account(db, True)
    out = await import_keyed_account_positions(db, _Adapter([_pos("ARBUSDT", "Sell", 13593.2)]), {dep})
    assert out["adopted"] == 1
    (row,) = await _rows(db)
    assert (row.symbol, row.side, row.size) == ("ARBUSDT", "SHORT", 13593.2)
    assert (row.deployment_id, row.status) == (dep, "OPEN")
    # real age from the exchange, not adoption time — else hold-time analytics are corrupted
    assert row.opened_at == datetime.fromtimestamp(1754000000, tz=UTC)


async def test_refuses_to_guess_owner_when_ambiguous(db):
    """A wrong owner is worse than a known gap. Two candidate deployments => SKIP and count."""
    deps = await _account(db, True, True)
    out = await import_keyed_account_positions(db, _Adapter([_pos("ARBUSDT", "Sell", 10)]), set(deps))
    assert out["ambiguous"] == 1 and out["adopted"] == 0
    assert await _rows(db) == []


async def test_adopts_for_a_paused_deployment(db):
    """A paused deployment is exactly the state you pause into while repairing; its book must still heal."""
    (dep,) = await _account(db, False)
    out = await import_keyed_account_positions(db, _Adapter([_pos("ARBUSDT", "Sell", 10)]), {dep})
    assert out["adopted"] == 1


async def test_enabled_deployment_wins_over_paused_ones(db):
    deps = await _account(db, True, False)
    out = await import_keyed_account_positions(db, _Adapter([_pos("ARBUSDT", "Sell", 10)]), set(deps))
    assert out["adopted"] == 1 and (await _rows(db))[0].deployment_id == deps[0]


async def test_exchange_read_failure_writes_nothing(db):
    """FAIL CLOSED. A failed view of the exchange must never drive a write."""
    (dep,) = await _account(db, True)
    await _add(db, symbol="ARBUSDT", side="SHORT", size=5, entry_price=1, deployment_id=dep)
    out = await import_keyed_account_positions(db, _Adapter(raises=True), {dep})
    assert out["errors"] == 1 and out["adopted"] == 0
    (row,) = await _rows(db)
    assert row.size == 5


async def test_corrects_stale_size(db):
    """Nothing else refreshes size on a keyed account; the exchange is authority."""
    (dep,) = await _account(db, True)
    await _add(db, symbol="LAUSDT", side="SHORT", size=13179.7, entry_price=1.0, deployment_id=dep)
    out = await import_keyed_account_positions(db, _Adapter([_pos("LAUSDT", "Sell", 26359.0, avg=2.5)]), {dep})
    assert out["size_fixed"] == 1
    (row,) = await _rows(db)
    assert (row.size, row.entry_price) == (26359.0, 2.5)


async def test_repairs_a_side_flip(db):
    (dep,) = await _account(db, True)
    await _add(db, symbol="OPUSDT", side="LONG", size=3, entry_price=1.0, deployment_id=dep)
    out = await import_keyed_account_positions(db, _Adapter([_pos("OPUSDT", "Sell", 4, avg=1.2)]), {dep})
    assert out["side_fixed"] == 1
    (row,) = await _rows(db)
    assert (row.side, row.size, row.entry_price) == ("SHORT", 4, 1.2)


async def test_dry_run_reports_without_writing(db):
    (dep,) = await _account(db, True)
    out = await import_keyed_account_positions(db, _Adapter([_pos("ARBUSDT", "Sell", 10)]), {dep}, dry_run=True)
    assert out["adopted"] == 1          # reported...
    assert await _rows(db) == []        # ...but nothing written


async def test_no_deployments_is_a_noop(db):
    out = await import_keyed_account_positions(db, _Adapter([_pos("X", "Buy", 1)]), set())
    assert out == {"exchange": 0, "adopted": 0, "size_fixed": 0, "side_fixed": 0,
                   "ambiguous": 0, "skipped_conflict": 0, "errors": 0}


async def test_matched_book_changes_nothing(db):
    """The steady state: DB agrees with the exchange -> no adoption, no correction, no churn."""
    (dep,) = await _account(db, True)
    await _add(db, symbol="ARBUSDT", side="SHORT", size=10.0, entry_price=100.0, deployment_id=dep)
    out = await import_keyed_account_positions(db, _Adapter([_pos("ARBUSDT", "Sell", 10.0)]), {dep})
    assert out["adopted"] == 0 and out["size_fixed"] == 0 and out["side_fixed"] == 0
    assert len(await _rows(db)) == 1


async def test_paper_positions_are_ignored_and_never_collide(db):
    (dep,) = await _account(db, True)
    await _add(db, symbol="ARBUSDT", side="SHORT", size=99.0, entry_price=1.0, deployment_id=dep, dry_run=True)
    out = await import_keyed_account_positions(db, _Adapter([_pos("ARBUSDT", "Sell", 10.0)]), {dep})
    assert out["adopted"] == 1 and out["size_fixed"] == 0     # the paper row is not "the" row
    rows = await _rows(db)
    assert sorted((r.dry_run, r.size) for r in rows) == [(False, 10.0), (True, 99.0)]
