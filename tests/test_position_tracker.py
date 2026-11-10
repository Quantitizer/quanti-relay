from datetime import UTC, datetime

import pytest

from relay.db.models import Account, Deployment
from relay.execution.order_link import make_order_link, parse_order_link
from relay.execution.position_tracker import PositionTracker


class _FakeBybit:
    def __init__(self, positions=(), closed_pnl=None, history=None, fail_positions=False):
        self.positions = list(positions)
        self.closed_pnl = closed_pnl or {}
        self.history = history or {}
        self.fail_positions = fail_positions

    async def get_positions(self, category="linear", symbol=None, settle_coin=None):
        if self.fail_positions:
            raise RuntimeError("venue down")
        return [p for p in self.positions if symbol is None or p["symbol"] == symbol]

    async def get_closed_pnl(self, category="linear", symbol=None, limit=5, start_time=None, max_pages=1):
        return self.closed_pnl.get(symbol, [])

    async def get_ticker(self, symbol, category="linear"):
        return {"lastPrice": "105"}

    async def get_order_history(self, symbol=None, limit=20, **kw):
        return self.history.get(symbol, [])


def _pos(symbol, side, size, avg="100", stop=""):
    return {"symbol": symbol, "side": side, "size": str(size), "avgPrice": avg,
            "markPrice": avg, "unrealisedPnl": "1.5", "stopLoss": stop}


async def _setup(db, keyed=False):
    """A default-account deployment (and optionally one on a keyed account)."""
    async with db.session() as s:
        default = Account(name="default")
        s.add(default)
        await s.flush()
        d = Deployment(name="main", account_id=default.id)
        s.add(d)
        k = None
        if keyed:
            acc = Account(name="sub", credentials_ref="SUB_ACCOUNT_1")
            s.add(acc)
            await s.flush()
            k = Deployment(name="sub", account_id=acc.id)
            s.add(k)
        await s.commit()
        return d.id, (k.id if k else None)


async def _open(db, symbol, side, deployment_id=None, entry=100.0, size=1.0):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        return await c.fetchval(
            "INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, opened_at,"
            " created_at, updated_at) VALUES ($1, $2, $3, $4, $5, 'OPEN', $6, $6, $6) RETURNING id",
            deployment_id, symbol, side, size, entry, now)


async def _rows(db, sql, *args):
    async with db.acquire() as c:
        return await c.fetch(sql, *args)


async def _row(db, pid):
    async with db.acquire() as c:
        return await c.fetchrow("SELECT * FROM positions WHERE id = $1", pid)


# ---- sync_all_positions ------------------------------------------------------------

def _venue_close(pnl="12.5", size="1", side="Sell", exit_px="112.7", order_id="c-1"):
    import time
    return {"symbol": "BTCUSDT", "side": side, "closedSize": size, "avgEntryPrice": "100.2", "closedPnl": pnl,
            "avgExitPrice": exit_px, "orderId": order_id, "updatedTime": str(int(time.time() * 1000))}


async def test_closed_on_exchange_takes_the_venues_complete_record(db):
    pid = await _open(db, "BTCUSDT", "LONG")
    r = await PositionTracker(db, _FakeBybit(closed_pnl={"BTCUSDT": [_venue_close()]})).sync_all_positions()
    assert r["closed"] == 1
    row = await _row(db, pid)
    assert row["status"] == "CLOSED" and row["pnl"] == 12.5 and row["exit_price"] == 112.7
    assert row["pnl_source"] == "venue_api" and row["close_order_id"] == "c-1"
    assert row["pnl_percent"] == 12.5 / 100.0 * 100            # % of the position's value


@pytest.mark.parametrize("record", [_venue_close(size="0.4"), _venue_close(side="Buy")])
async def test_an_incomplete_or_foreign_record_leaves_the_close_pending(db, record):
    pid = await _open(db, "BTCUSDT", "LONG")
    await PositionTracker(db, _FakeBybit(closed_pnl={"BTCUSDT": [record]})).sync_all_positions()
    row = await _row(db, pid)
    assert (row["status"], row["pnl_source"], row["pnl"], row["exit_price"]) == ("CLOSED", "pending", None, None)
    assert row["close_order_id"] is None                          # never borrow another close's order id


async def test_no_close_record_leaves_the_close_pending_instead_of_guessing(db):
    pid = await _open(db, "BTCUSDT", "SHORT")
    await PositionTracker(db, _FakeBybit()).sync_all_positions()
    row = await _row(db, pid)
    assert row["pnl_source"] == "pending" and row["exit_price"] is None and row["pnl"] is None
    (t,) = await _rows(db, "SELECT exit_reason, trade_type FROM trades WHERE position_id = $1", pid)
    assert (t["trade_type"], t["exit_reason"]) == ("exit", "closed_on_venue")


async def test_still_open_refreshes_unrealized_pnl(db):
    pid = await _open(db, "BTCUSDT", "LONG")
    r = await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1)])).sync_all_positions()
    assert r["updated"] == 1 and r["closed"] == 0
    row = await _row(db, pid)
    assert row["status"] == "OPEN" and row["unrealized_pnl"] == 1.5


async def test_exchange_read_failure_closes_nothing(db):
    pid = await _open(db, "BTCUSDT", "LONG")
    r = await PositionTracker(db, _FakeBybit(fail_positions=True)).sync_all_positions()
    assert r["errors"] == 1
    assert (await _row(db, pid))["status"] == "OPEN"


async def test_a_flipped_side_closes_the_old_side_and_the_import_adopts_the_new_one(db):
    """DB long, venue short: the long is gone (closed on the venue), the short is a new position."""
    pid = await _open(db, "BTCUSDT", "LONG")
    tracker = PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Sell", 1)]))
    r = await tracker.sync_all_positions()
    assert r["side_mismatch"] == 1 and r["closed"] == 1
    assert (await _row(db, pid))["status"] == "CLOSED"
    imported = await tracker.import_exchange_positions()
    assert imported["imported_count"] == 1
    (short,) = await _rows(db, "SELECT side, status FROM positions WHERE id <> $1", pid)
    assert (short["side"], short["status"]) == ("SHORT", "OPEN")


async def test_default_sync_never_touches_a_keyed_account(db):
    main, sub = await _setup(db, keyed=True)
    mine = await _open(db, "BTCUSDT", "LONG", main)
    theirs = await _open(db, "ETHUSDT", "LONG", sub)
    await PositionTracker(db, _FakeBybit()).sync_all_positions()  # default account holds nothing
    assert (await _row(db, mine))["status"] == "CLOSED"
    assert (await _row(db, theirs))["status"] == "OPEN"


async def test_scoped_sync_touches_only_its_deployments(db):
    main, sub = await _setup(db, keyed=True)
    mine = await _open(db, "BTCUSDT", "LONG", main)
    theirs = await _open(db, "ETHUSDT", "LONG", sub)
    await PositionTracker(db, _FakeBybit()).sync_all_positions(
        client=_FakeBybit(), scope_deployment_ids={sub})
    assert (await _row(db, mine))["status"] == "OPEN"
    assert (await _row(db, theirs))["status"] == "CLOSED"


# ---- import_exchange_positions -----------------------------------------------------

async def test_import_adopts_untracked_position_with_its_stop(db):
    fake = _FakeBybit([_pos("SOLUSDT", "Sell", "-3", avg="150", stop="160")])
    r = await PositionTracker(db, fake).import_exchange_positions()
    assert r["imported_symbols"] == ["SOLUSDT"]
    async with db.acquire() as c:
        p = await c.fetchrow("SELECT * FROM positions")
        t = await c.fetchrow("SELECT * FROM trades")
    assert (p["side"], p["size"], p["entry_price"], p["initial_stop_price"]) == ("SHORT", 3, 150, 160)
    assert p["deployment_id"] is None
    assert (t["side"], t["trade_type"], t["stop_loss_price"]) == ("SELL", "import", 160)


async def test_import_without_stop_leaves_it_unset(db):
    await PositionTracker(db, _FakeBybit([_pos("SOLUSDT", "Buy", 1, stop="0.00")])).import_exchange_positions()
    async with db.acquire() as c:
        assert await c.fetchval("SELECT initial_stop_price FROM positions") is None


async def test_import_attributes_relay_orders_to_their_deployment(db):
    main, _ = await _setup(db)
    hist = {"BTCUSDT": [{"side": "Buy", "orderLinkId": "someone-else"},
                        {"side": "Buy", "orderLinkId": make_order_link(main)}],
            "ETHUSDT": [{"side": "Buy", "orderLinkId": make_order_link(999)}]}  # unknown deployment -> unattributed
    fake = _FakeBybit([_pos("BTCUSDT", "Buy", 1), _pos("ETHUSDT", "Buy", 1)], history=hist)
    await PositionTracker(db, fake).import_exchange_positions()
    async with db.acquire() as c:
        rows = {r["symbol"]: r["deployment_id"] for r in await c.fetch("SELECT symbol, deployment_id FROM positions")}
    assert rows == {"BTCUSDT": main, "ETHUSDT": None}


async def test_import_is_idempotent_and_reports_already_tracked(db):
    fake = _FakeBybit([_pos("BTCUSDT", "Buy", 1), _pos("ETHUSDT", "Buy", 2)])
    t = PositionTracker(db, fake)
    first = await t.import_exchange_positions()
    # Regression: the per-position dedup lookup overwrote this count with its last result (None).
    assert first["imported_count"] == 2 and first["already_tracked"] == 0
    again = await t.import_exchange_positions()
    assert again["imported_count"] == 0 and again["already_tracked"] == 2
    async with db.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM positions") == 2


async def test_keyed_account_row_does_not_block_default_import(db):
    _, sub = await _setup(db, keyed=True)
    await _open(db, "BTCUSDT", "LONG", sub)
    r = await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1)])).import_exchange_positions()
    assert r["imported_count"] == 1


async def test_partially_tracked_symbol_imports_only_the_missing_part(db):
    """Dedup is per (symbol, side) within the default account."""
    pid = await _open(db, "BTCUSDT", "LONG")
    fake = _FakeBybit([_pos("BTCUSDT", "Buy", 1), _pos("ETHUSDT", "Sell", 1)])
    r = await PositionTracker(db, fake).import_exchange_positions()
    assert r["imported_symbols"] == ["ETHUSDT"]
    assert (await _row(db, pid))["status"] == "OPEN"


def test_order_link_roundtrip():
    link = make_order_link(42)
    assert len(link) <= 36 and parse_order_link(link) == 42
    assert parse_order_link("other-d3-abc") is None and parse_order_link(None) is None
    assert parse_order_link(make_order_link(None)) == 0


async def test_other_venue_row_does_not_block_default_import(db):
    async with db.session() as s:
        hl = Account(name="hl", venue="hyperliquid")  # no credentials_ref, but not the Bybit account
        s.add(hl)
        await s.flush()
        dep = Deployment(name="hl-book", account_id=hl.id)
        s.add(dep)
        await s.commit()
    await _open(db, "BTCUSDT", "LONG", dep.id)
    r = await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1)])).import_exchange_positions()
    assert r["imported_count"] == 1


# ---- paper (dry-run) positions are never reconciled against the exchange -----------

async def _paper(db, symbol, side, deployment_id=None):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        return await c.fetchval(
            "INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, dry_run,"
            " opened_at, created_at, updated_at) VALUES ($1, $2, $3, 1, 100, 'OPEN', TRUE, $4, $4, $4)"
            " RETURNING id", deployment_id, symbol, side, now)


async def test_sync_never_closes_a_paper_position(db):
    pid = await _paper(db, "BTCUSDT", "LONG")
    r = await PositionTracker(db, _FakeBybit()).sync_all_positions()  # nothing on the exchange
    assert r["closed"] == 0
    assert (await _row(db, pid))["status"] == "OPEN"


async def test_paper_position_does_not_block_importing_a_real_one(db):
    main, _ = await _setup(db)
    await _paper(db, "BTCUSDT", "LONG", main)
    hist = {"BTCUSDT": [{"side": "Buy", "orderLinkId": make_order_link(main)}]}  # same deployment, same side
    r = await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Buy", 1)], history=hist)).import_exchange_positions()
    assert r["imported_count"] == 1
    async with db.acquire() as c:
        rows = await c.fetch("SELECT deployment_id, dry_run FROM positions ORDER BY id")
    assert [(x["deployment_id"], bool(x["dry_run"])) for x in rows] == [(main, True), (main, False)]


async def test_close_keeps_a_recorded_leverage_and_never_invents_one(db):
    """No user leverage configured and no venue record: a recorded leverage survives the close,
    a missing one stays missing (PnL% is then the % of notional)."""
    known = await _open(db, "BTCUSDT", "SHORT")
    unknown = await _open(db, "ETHUSDT", "SHORT")
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET leverage = 3 WHERE id = $1", known)
    await PositionTracker(db, _FakeBybit()).sync_all_positions()     # both gone from the venue
    k, u = await _row(db, known), await _row(db, unknown)
    assert k["status"] == u["status"] == "CLOSED"
    assert k["leverage"] == 3 and u["leverage"] is None          # nothing written over or invented


async def _set_status(db, pid, status):
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = $1 WHERE id = $2", status, pid)


async def test_a_closing_row_gone_from_the_venue_is_closed(db):
    pid = await _open(db, "BTCUSDT", "LONG")
    await _set_status(db, pid, "CLOSING")                       # a fast close whose bookkeeping never finished
    await PositionTracker(db, _FakeBybit()).sync_all_positions()
    assert (await _row(db, pid))["status"] == "CLOSED"


async def test_a_closing_row_still_on_the_venue_goes_back_to_open(db):
    pid = await _open(db, "BTCUSDT", "LONG")
    await _set_status(db, pid, "CLOSING")                       # the close never reached the venue
    await PositionTracker(db, _FakeBybit(positions=[_pos("BTCUSDT", "Buy", 1)])).sync_all_positions()
    row = await _row(db, pid)
    assert row["status"] == "OPEN" and row["unrealized_pnl"] == 1.5


async def test_import_attribution_ignores_closing_and_opposite_side_orders(db):
    main, _ = await _setup(db)
    async with db.session() as sess:
        other = Deployment(name="other", account_id=(await sess.get(Deployment, main)).account_id)
        sess.add(other)
        await sess.commit()
        other_id = other.id
    hist = {"BTCUSDT": [
        {"side": "Sell", "reduceOnly": True, "orderLinkId": make_order_link(other_id)},   # a close
        {"side": "Sell", "orderLinkId": make_order_link(other_id)},                       # the other direction
        {"side": "Buy", "orderLinkId": make_order_link(main)},                            # the opening order
    ]}
    fake = _FakeBybit([_pos("BTCUSDT", "Buy", 1)], history=hist)
    await PositionTracker(db, fake).import_exchange_positions()
    (row,) = await _rows(db, "SELECT deployment_id, entry_meta FROM positions")
    assert row["deployment_id"] == main


async def test_an_import_without_a_venue_entry_price_is_labelled(db):
    import json
    p = {**_pos("BTCUSDT", "Buy", 1), "avgPrice": "0", "markPrice": "101"}
    await PositionTracker(db, _FakeBybit([p])).import_exchange_positions()
    (row,) = await _rows(db, "SELECT entry_price, entry_meta FROM positions")
    meta = row["entry_meta"] if isinstance(row["entry_meta"], dict) else json.loads(row["entry_meta"])
    assert row["entry_price"] == 101.0 and meta["entry_price_source"] == "mark_at_import"


async def test_a_new_side_is_imported_while_the_old_side_is_still_closing(db):
    pid = await _open(db, "BTCUSDT", "LONG")
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", pid)
    r = await PositionTracker(db, _FakeBybit([_pos("BTCUSDT", "Sell", 1)])).import_exchange_positions()
    assert r["imported_count"] == 1
