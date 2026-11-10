"""The window PnL reconciler: venue-verified PnL for closed live rows, never guessed."""
from datetime import UTC, datetime, timedelta

import pytest

from relay.db.models import Account, Deployment, Position
from relay.execution.pnl_reconciler import reconcile_closed_pnl


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


class _FakeBybit:
    def __init__(self, closed=None, txlog=None, txlog_fails=False):
        self.closed = closed or {}
        self.txlog = txlog or []
        self.txlog_fails = txlog_fails
        self.max_pages = None

    async def get_closed_pnl(self, category="linear", symbol=None, limit=50, start_time=None, max_pages=1):
        self.max_pages = max_pages
        return self.closed.get(symbol, [])

    async def get_transaction_log(self, category="linear", start_time=None):
        if self.txlog_fails:
            raise RuntimeError("rate limited")
        return self.txlog


async def _closed_row(db, symbol, closed_at, pnl_source="pending", deployment_id=None, opened_at=None,
                      side="LONG", **kw):
    async with db.session() as s:
        p = Position(symbol=symbol, side=side, size=kw.pop("size", 1), entry_price=kw.pop("entry", 2000.0),
                     status="CLOSED",
                     opened_at=opened_at or closed_at - timedelta(hours=1), closed_at=closed_at,
                     pnl_source=pnl_source, deployment_id=deployment_id, **kw)
        s.add(p)
        await s.commit()
        return p.id


async def _get(db, pid):
    async with db.session() as s:
        return await s.get(Position, pid)


async def _keyed_deployment(db):
    async with db.session() as s:
        acc = Account(name="sub", credentials_ref="SUB_ACCOUNT_1")
        s.add(acc)
        await s.flush()
        dep = Deployment(name="sub", account_id=acc.id)
        s.add(dep)
        await s.commit()
        return dep.id


NOW = datetime.now(UTC)
OPENED, CLOSED = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
TRIM = {"orderId": "t1", "side": "Sell", "closedSize": "0.4", "updatedTime": _ms(OPENED + timedelta(hours=1)), "closedPnl": "4",
        "avgExitPrice": "2010"}
FINAL = {"orderId": "f1", "side": "Sell", "closedSize": "0.6", "updatedTime": _ms(CLOSED), "closedPnl": "6", "avgExitPrice": "2030"}
FUNDING = {"type": "SETTLEMENT", "symbol": "ETHUSDT", "transactionTime": _ms(OPENED + timedelta(minutes=30)),
           "change": "-0.75"}


async def test_dry_run_reports_and_writes_nothing(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED)
    report = await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [TRIM, FINAL]}, [FUNDING]))
    assert report["dry_run"] and len(report["reconciled"]) == 1 and report["written"] == 0
    assert (await _get(db, pid)).pnl_source == "pending"


async def test_window_sum_writes_pnl_including_trims_and_funding(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, pnl_source="local_calc")
    report = await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [TRIM, FINAL]}, [FUNDING]), dry_run=False)
    assert report["written"] == 1
    row = await _get(db, pid)
    assert (row.pnl, row.funding, row.exit_price, row.pnl_source) == (10.0, -0.75, 2030.0, "venue_api")


async def test_only_closes_in_the_positions_direction_count(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, size=0.6)
    other_direction = {**TRIM, "orderId": "x1", "side": "Buy", "closedPnl": "-50"}   # closes a SHORT
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [other_direction, FINAL]}, [FUNDING]), dry_run=False)
    assert (await _get(db, pid)).pnl == 6.0


async def test_an_order_already_attributed_to_a_verified_row_is_not_counted_again(db):
    await _closed_row(db, "ETHUSDT", CLOSED - timedelta(minutes=30), opened_at=OPENED, pnl_source="venue_api",
                      pnl=4.0, funding=0.0, close_order_id="t1")
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, size=0.6)
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [TRIM, FINAL]}, [FUNDING]), dry_run=False)
    assert (await _get(db, pid)).pnl == 6.0


async def test_an_unreadable_funding_log_leaves_funding_unknown_not_zero(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, size=0.6)
    report = await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [FINAL]}, txlog_fails=True), dry_run=False)
    row = await _get(db, pid)
    assert report["funding_unavailable"] and row.pnl == 6.0 and row.funding is None   # retried next pass


async def test_a_verified_row_missing_funding_gets_funding_and_keeps_its_pnl(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, pnl_source="venue_api", pnl=99.0)
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [TRIM, FINAL]}, [FUNDING]), dry_run=False)
    row = await _get(db, pid)
    assert row.pnl == 99.0 and row.funding == -0.75


async def test_rows_without_a_venue_record_are_left_for_a_later_pass(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED)
    report = await reconcile_closed_pnl(db, _FakeBybit(), dry_run=False)
    assert report["written"] == 0 and (await _get(db, pid)).pnl_source == "pending"


async def test_closed_pnl_is_fetched_across_pages(db):
    await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED)
    fake = _FakeBybit({"ETHUSDT": [FINAL]})
    await reconcile_closed_pnl(db, fake)
    assert fake.max_pages > 1


async def test_the_default_reconciler_never_touches_a_keyed_account(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, deployment_id=await _keyed_deployment(db))
    report = await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [FINAL]}), dry_run=False)
    assert report["scanned"] == 0 and (await _get(db, pid)).pnl_source == "pending"


async def test_paper_positions_are_ignored(db):
    async with db.session() as s:
        s.add(Position(symbol="ETHUSDT", side="LONG", size=1, entry_price=2000.0, status="CLOSED", dry_run=True,
                       opened_at=OPENED, closed_at=CLOSED, pnl_source="pending"))
        await s.commit()
    assert (await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [FINAL]}), dry_run=False))["scanned"] == 0


async def test_an_incomplete_window_is_not_priced(db):
    """Only the 0.4 trim is published so far: summing it as the whole result would be wrong."""
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED)
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [TRIM]}, [FUNDING]), dry_run=False)
    assert (await _get(db, pid)).pnl_source == "pending"


async def test_a_relay_partial_close_counts_toward_completeness(db):
    """The row's size is what was left at the close (0.6); its recorded partial (0.4) makes 1.0."""
    from relay.db.models import Trade
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, size=0.6)
    async with db.session() as s:
        s.add(Trade(position_id=pid, symbol="ETHUSDT", side="SELL", qty=0.4, price=2010.0,
                    time=OPENED + timedelta(hours=1), trade_type="partial_exit"))
        await s.commit()
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [TRIM, FINAL]}, [FUNDING]), dry_run=False)
    assert (await _get(db, pid)).pnl == 10.0


async def test_the_reconciler_also_sets_the_pnl_percent(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, size=0.6, entry=2000.0)
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [FINAL]}, [FUNDING]), dry_run=False)
    row = await _get(db, pid)
    assert row.pnl == 6.0 and row.pnl_percent == 6.0 / (2000.0 * 0.6) * 100


async def test_records_without_a_closed_size_are_not_trusted_as_complete(db):
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, size=0.6)
    sizeless = {k: v for k, v in FINAL.items() if k != "closedSize"}
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [sizeless]}, [FUNDING]), dry_run=False)
    assert (await _get(db, pid)).pnl_source == "pending"


def test_the_reconciler_writes_by_default_and_runs_often():
    """Writes, and soon: an unpriced live close blocks a daily loss limit until it is priced."""
    from relay.config import Settings
    s = Settings(_env_file=None)
    assert s.pnl_reconcile_writes is True and s.pnl_reconcile_interval_s <= 120


async def test_an_unpriced_partial_is_priced_from_the_venue_record_of_its_own_order(db):
    from relay.db.models import Trade
    from relay.execution.pnl_reconciler import reconcile_partial_pnl
    async with db.session() as s:
        p = Position(symbol="ETHUSDT", side="LONG", size=0.6, entry_price=2000.0, status="OPEN", opened_at=OPENED)
        s.add(p)
        await s.flush()
        t = Trade(position_id=p.id, symbol="ETHUSDT", side="SELL", qty=0.4, price=None, order_id="t1",
                  time=OPENED + timedelta(hours=1), trade_type="partial_exit", metadata_={"pnl": None})
        s.add(t)
        await s.commit()
        tid = t.id
    other = {**TRIM, "orderId": "zzz", "closedPnl": "-99"}         # a different order: never borrowed
    report = await reconcile_partial_pnl(db, _FakeBybit({"ETHUSDT": [other, TRIM]}), dry_run=False)
    assert report["written"] == 1
    async with db.session() as s:
        t = await s.get(Trade, tid)
        assert t.metadata_["pnl"] == 4.0 and t.price == 2010.0


async def test_partial_pricing_never_writes_a_missing_pnl_as_zero_and_checks_the_quantity(db):
    from relay.db.models import Trade
    from relay.execution.pnl_reconciler import reconcile_partial_pnl
    async with db.session() as s:
        p = Position(symbol="ETHUSDT", side="LONG", size=0.6, entry_price=2000.0, status="OPEN", opened_at=OPENED)
        s.add(p)
        await s.flush()
        for oid in ("no-pnl", "wrong-qty", "split"):
            s.add(Trade(position_id=p.id, symbol="ETHUSDT", side="SELL", qty=0.4, price=None, order_id=oid,
                        time=OPENED + timedelta(hours=1), trade_type="partial_exit", metadata_={"pnl": None}))
        await s.commit()
    recs = [{**TRIM, "orderId": "no-pnl", "closedPnl": ""},
            {**TRIM, "orderId": "wrong-qty", "closedSize": "0.1"},
            {**TRIM, "orderId": "split", "closedSize": "0.25", "closedPnl": "3"},
            {**TRIM, "orderId": "split", "closedSize": "0.15", "closedPnl": "1"}]
    report = await reconcile_partial_pnl(db, _FakeBybit({"ETHUSDT": recs}), dry_run=False)
    assert [x["pnl"] for x in report["priced"]] == [4.0]           # only the complete, split order


async def test_the_pnl_percent_of_a_position_added_to_uses_its_real_cost(db):
    from relay.db.models import Trade
    # 1.0 @ 1500; 0.4 closed; add 0.4 @ 2250 -> holds 1.0 at an average of 1800.
    # Real cost 1500 + 900 = 2400; "average x everything traded" would say 1800 x 1.4 = 2520.
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, size=1.0, entry=1800.0)
    async with db.session() as s:
        s.add_all([Trade(position_id=pid, symbol="ETHUSDT", trade_type="entry", side="BUY", qty=1.0, price=1500.0,
                         time=OPENED),
                   Trade(position_id=pid, symbol="ETHUSDT", trade_type="partial_exit", side="SELL", qty=0.4,
                         price=2010.0, time=OPENED + timedelta(hours=1)),
                   Trade(position_id=pid, symbol="ETHUSDT", trade_type="entry", side="BUY", qty=0.4, price=2250.0,
                         time=OPENED + timedelta(hours=1, minutes=30))])
        await s.commit()
    final = {**FINAL, "closedSize": "1.0"}
    await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [TRIM, final]}, [FUNDING]), dry_run=False)
    row = await _get(db, pid)
    assert row.pnl == 10.0 and row.pnl_percent == 10.0 / 2400.0 * 100


async def _venue_reduce(db, *, with_ladder_order=None):
    """A live long, a relay entry trade, and a 0.4 partial the drift repair recorded for a reduce
    made by hand on the venue (no order of the relay's own)."""
    from relay.db.models import Trade
    async with db.session() as s:
        mgmt = ({"take_profits": {"levels": [{"n": 1, "status": "open", "order_id": with_ladder_order}]}}
                if with_ladder_order else None)
        p = Position(symbol="ETHUSDT", side="LONG", size=0.6, entry_price=2000.0, status="OPEN", opened_at=OPENED,
                     dry_run=False, management=mgmt)
        s.add(p)
        await s.flush()
        s.add(Trade(position_id=p.id, symbol="ETHUSDT", side="BUY", qty=1.0, price=2000.0, order_id="e1",
                    time=OPENED, trade_type="entry", metadata_={}))
        t = Trade(position_id=p.id, symbol="ETHUSDT", side="SELL", qty=0.4, price=None, order_id=None,
                  time=OPENED + timedelta(hours=2), trade_type="partial_exit", exit_reason="reduced_on_venue",
                  metadata_={"pnl": None})
        s.add(t)
        await s.commit()
        return t.id


ENTRY_REC = {"orderId": "e1", "side": "Buy", "closedSize": "0.4", "updatedTime": _ms(OPENED), "closedPnl": "0",
             "avgExitPrice": "2000"}


async def test_a_reduce_made_on_the_venue_is_priced_from_the_orders_no_record_accounts_for(db):
    from relay.db.models import Trade
    from relay.execution.pnl_reconciler import reconcile_partial_pnl
    tid = await _venue_reduce(db)
    hand = [{"orderId": "hand-1", "side": "Sell", "closedSize": "0.1", "updatedTime": _ms(OPENED + timedelta(hours=1)),
             "closedPnl": "1", "avgExitPrice": "2010"},
            {"orderId": "hand-2", "side": "Sell", "closedSize": "0.3", "updatedTime": _ms(OPENED + timedelta(hours=1, minutes=5)),
             "closedPnl": "6", "avgExitPrice": "2020"}]
    late = {"orderId": "hand-3", "side": "Sell", "closedSize": "0.4", "updatedTime": _ms(OPENED + timedelta(hours=2, minutes=1)),
            "closedPnl": "-50", "avgExitPrice": "1900"}                  # after the repair: not this one
    wrong_side = {**hand[0], "orderId": "hand-4", "side": "Buy", "closedPnl": "-70"}
    report = await reconcile_partial_pnl(db, _FakeBybit({"ETHUSDT": [ENTRY_REC, *hand, late, wrong_side]}),
                                         dry_run=False)
    assert report["written"] == 1
    async with db.session() as s:
        t = await s.get(Trade, tid)
        assert t.metadata_["pnl"] == 7.0 and t.metadata_["pnl_source"] == "venue_api"
        assert sorted(t.metadata_["venue_orders"]) == ["hand-1", "hand-2"]
        assert t.price == pytest.approx((0.1 * 2010 + 0.3 * 2020) / 0.4)
    again = await reconcile_partial_pnl(db, _FakeBybit({"ETHUSDT": [ENTRY_REC, *hand]}), dry_run=False)
    assert again["written"] == 0                                       # priced once


@pytest.mark.parametrize("records", [
    [],                                                                # nothing published yet
    [{"orderId": "hand-1", "side": "Sell", "closedSize": "0.3", "closedPnl": "6", "avgExitPrice": "2020"}],   # too little
    [{"orderId": "hand-1", "side": "Sell", "closedSize": "0.4", "closedPnl": "", "avgExitPrice": "2020"}],    # no PnL yet
    [{"orderId": "tp1", "side": "Sell", "closedSize": "0.4", "closedPnl": "6", "avgExitPrice": "2020"}],  # the ladder's
])
async def test_a_venue_reduce_without_an_exact_match_waits(db, records):
    from relay.db.models import Trade
    from relay.execution.pnl_reconciler import reconcile_partial_pnl
    tid = await _venue_reduce(db, with_ladder_order="tp1")
    recs = [{**r, "updatedTime": _ms(OPENED + timedelta(hours=1))} for r in records]
    report = await reconcile_partial_pnl(db, _FakeBybit({"ETHUSDT": recs}), dry_run=False)
    assert report["written"] == 0
    async with db.session() as s:
        assert (await s.get(Trade, tid)).metadata_["pnl"] is None


async def test_one_venue_order_never_prices_two_reduces(db):
    from relay.db.models import Trade
    from relay.execution.pnl_reconciler import reconcile_partial_pnl
    first = await _venue_reduce(db)
    async with db.session() as s:
        t = await s.get(Trade, first)
        t.metadata_ = {"pnl": 7.0, "venue_orders": ["hand-1"]}
        (await s.get(Position, t.position_id)).status = "CLOSED"            # one open position per symbol
        await s.commit()
    second = await _venue_reduce(db)
    rec = {"orderId": "hand-1", "side": "Sell", "closedSize": "0.4", "updatedTime": _ms(OPENED + timedelta(hours=1)),
           "closedPnl": "7", "avgExitPrice": "2020"}
    assert (await reconcile_partial_pnl(db, _FakeBybit({"ETHUSDT": [rec]}), dry_run=False))["written"] == 0
    async with db.session() as s:
        assert (await s.get(Trade, second)).metadata_["pnl"] is None


async def test_a_reconciler_on_another_venue_never_prices_this_venues_rows(db):
    from relay.execution.pnl_reconciler import reconcile_partial_pnl
    async with db.session() as s:
        acc = Account(name="main", venue="bybit")
        s.add(acc)
        await s.flush()
        dep = Deployment(name="main", account_id=acc.id)
        s.add(dep)
        await s.commit()
        dep_id = dep.id
    pid = await _closed_row(db, "ETHUSDT", CLOSED, opened_at=OPENED, deployment_id=dep_id)
    other = _FakeBybit({"ETHUSDT": [FINAL]})
    other.venue_id = "okx"                                             # the client of another venue
    assert (await reconcile_closed_pnl(db, other, dry_run=False))["scanned"] == 0
    assert (await reconcile_partial_pnl(db, other, dry_run=False))["scanned"] == 0
    assert (await _get(db, pid)).pnl_source == "pending"
    report = await reconcile_closed_pnl(db, _FakeBybit({"ETHUSDT": [FINAL]}), dry_run=False)
    assert report["scanned"] == 1
