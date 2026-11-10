"""Execution engine: entry gates, dry-run paper positions, live order flow, exits, live feed, stop audit."""
import json
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from relay.db.models import Account, Deployment, Signal
from relay.exchange.bybit.private_ws import PositionUpdate
from relay.execution import engine as engine_mod
from relay.execution.engine import ExecutionEngine
from relay.execution.order_link import parse_order_link

SETTINGS = {"leverage": 2, "max_positions": 10, "max_exposure_pct": 100.0, "min_stop_distance_pct": 0.5}


_real_sleep = engine_mod.asyncio.sleep


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Short waits (retry back-offs) are instant; long ones (background-loop intervals) block until
    the task is cancelled, so the loops never spin."""
    async def _sleep(seconds):
        await _real_sleep(0 if seconds < 5 else 3600)
    monkeypatch.setattr(engine_mod.asyncio, "sleep", _sleep)
    import relay.execution.order_executor as oe
    monkeypatch.setattr(oe.asyncio, "sleep", _sleep)


class FakeBybit:
    """Records every call that would reach the exchange."""

    LOT: ClassVar[dict] = {"qtyStep": "0.01", "minOrderQty": "0.01", "maxOrderQty": "1000"}

    def __init__(self, price=100.0, balance=10_000.0, positions=(), stop_ok=True, closed_pnl=None, lot=None):
        self.price, self.balance = price, balance
        self.lot = self.LOT if lot is None else lot
        self.positions = list(positions)
        self.stop_ok = stop_ok
        self.closed_pnl = closed_pnl or {}
        self.calls = []

    def _rec(self, name, **kw):
        self.calls.append((name, kw))

    def names(self):
        return [c[0] for c in self.calls]

    async def get_ticker(self, symbol, category="linear"):
        return {"lastPrice": str(self.price)}

    async def list_instruments(self, category="linear", symbol=None, status=None):
        return [{"symbol": symbol, "lotSizeFilter": self.lot, "priceFilter": {"tickSize": "0.1"}}]

    async def get_balance_safe(self, coin="USDT"):
        return self.balance, "wallet"

    async def switch_isolated_margin(self, **kw):
        self._rec("switch_isolated_margin", **kw)
        return {"retCode": 0}

    async def set_leverage(self, **kw):
        self._rec("set_leverage", **kw)
        return {"retCode": 0}

    async def place_market_order(self, **kw):
        self._rec("place_market_order", **kw)
        return {"orderId": f"o{len(self.calls)}", "avgPrice": str(self.price), "cumExecQty": kw.get("qty")}

    async def set_trading_stop(self, **kw):
        self._rec("set_trading_stop", **kw)
        return {"retCode": 0} if self.stop_ok else {"retCode": 10001, "retMsg": "params error"}

    async def get_order_history(self, **kw):
        placed = [c for n, c in self.calls if n == "place_market_order"]
        qty = placed[-1].get("qty") if placed else None
        return [{"orderStatus": "Filled", "avgPrice": str(self.price), "cumExecQty": qty,
                 "orderId": f"o{len(self.calls)}"}]

    async def get_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        return None                                       # not in the real-time view: history

    async def get_positions(self, category="linear", symbol=None, settle_coin=None):
        return self.positions

    async def get_closed_pnl(self, category="linear", symbol=None, limit=5, **kw):
        self._rec("get_closed_pnl", symbol=symbol)
        return self.closed_pnl.get(symbol, [])

    async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        self._rec("cancel_order", symbol=symbol, order_id=order_id)
        return {}


async def _deployment(db):
    async with db.session() as s:
        a = Account(name="main")
        s.add(a)
        await s.flush()
        d = Deployment(name="main", account_id=a.id)
        s.add(d)
        await s.commit()
        return d.id


async def _engine(db, fake, dry_run=True, settings=None):
    dep = await _deployment(db)
    eng = ExecutionEngine(db, fake, settings or SETTINGS, dry_run=dry_run, deployment_id=dep)
    await eng.initialize()
    return eng, dep


def _sig(**kw):
    s = {"symbol": "BTCUSDT", "direction": "long", "size": {"notional_usd": 500}, "stop_price": 95.0,
         "reason": "test"}
    s.update(kw)
    return s


async def _rows(db, sql, *args):
    async with db.acquire() as c:
        return await c.fetch(sql, *args)


# ---- dry-run --------------------------------------------------------------------------

async def test_dry_run_entry_creates_a_paper_position_and_touches_nothing(db):
    fake = FakeBybit()
    eng, dep = await _engine(db, fake)
    r = await eng.execute_entry(_sig())
    assert r["success"] and r["dry_run"] is True and r["quantity"] == 5.0
    order_calls = {"place_market_order", "set_trading_stop", "set_leverage", "switch_isolated_margin"}
    assert not order_calls & set(fake.names())
    (p,) = await _rows(db, "SELECT * FROM positions")
    assert (p["deployment_id"], p["side"], p["size"], bool(p["dry_run"]), p["initial_stop_price"]) == \
        (dep, "LONG", 5.0, True, 95.0)
    (t,) = await _rows(db, "SELECT * FROM trades")
    assert (t["trade_type"], t["side"], t["stop_loss_price"]) == ("entry", "BUY", 95.0)


async def test_dry_run_exit_is_simulated_and_never_reads_real_pnl(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    r = await eng.execute_entry(_sig())
    fake.price = 110.0
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos, reason="signal")
    (p,) = await _rows(db, "SELECT * FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "CLOSED" and p["pnl_source"] == "dry_run" and p["pnl"] == pytest.approx(50.0)
    assert "get_closed_pnl" not in fake.names()


# ---- live flow ------------------------------------------------------------------------

async def test_live_entry_tags_order_sets_leverage_and_stop(db):
    fake = FakeBybit()
    eng, dep = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(direction="short", stop_price=104.97))
    assert r["success"] and r["dry_run"] is False
    names = fake.names()
    assert names.index("set_leverage") < names.index("place_market_order") < names.index("set_trading_stop")
    order = dict(fake.calls)["place_market_order"]
    assert order["side"] == "Sell" and parse_order_link(order["order_link_id"]) == dep
    stop = dict(fake.calls)["set_trading_stop"]
    assert stop["stop_loss"] == pytest.approx(104.9)  # tick-aligned toward entry: risk can only shrink


async def test_no_leverage_configured_leaves_the_account_untouched(db):
    """No shipped leverage: unless the user sets one, a live entry never changes the account's
    leverage or margin mode, and the position records none."""
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={k: v for k, v in SETTINGS.items() if k != "leverage"})
    r = await eng.execute_entry(_sig())
    assert r["success"]
    assert "set_leverage" not in fake.names() and "switch_isolated_margin" not in fake.names()
    async with db.acquire() as c:
        assert await c.fetchval("SELECT leverage FROM positions WHERE id = $1", r["position_id"]) is None


async def test_no_minimum_balance_unless_configured(db):
    fake = FakeBybit(balance=50.0)
    eng, _ = await _engine(db, fake)
    assert (await eng.execute_entry(_sig(size={"notional_usd": 20})))["success"]


def _venue_close(closed_pnl="42", size="5", side="Sell", exit_px="108.5", order_id="x1"):
    import time
    return {"symbol": "BTCUSDT", "side": side, "closedSize": size, "avgEntryPrice": "100", "closedPnl": closed_pnl,
            "avgExitPrice": exit_px, "orderId": order_id,
            "updatedTime": str(int(time.time() * 1000) + 1000)}         # published just after the position opens


async def test_live_exit_takes_the_venues_complete_record(db):
    fake = FakeBybit(closed_pnl={"BTCUSDT": [_venue_close()]})
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    (p,) = await _rows(db, "SELECT * FROM positions WHERE id = $1", r["position_id"])
    assert (p["pnl"], p["exit_price"], p["pnl_source"]) == (42.0, 108.5, "venue_api")
    assert p["pnl_percent"] == pytest.approx(42 / 500 * 100)       # % of the position's value, no leverage factor
    (t,) = await _rows(db, "SELECT exit_reason, trade_type FROM trades WHERE position_id = $1 AND trade_type = 'exit'",
                       r["position_id"])
    assert t["exit_reason"] == "manual"


@pytest.mark.parametrize("record", [
    _venue_close(size="2"),             # the venue has not published the whole close yet
    _venue_close(side="Buy"),           # a close of a SHORT: not this position's
])
async def test_an_incomplete_or_foreign_venue_record_is_not_trusted(db, record):
    fake = FakeBybit(closed_pnl={"BTCUSDT": [record]})
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    (p,) = await _rows(db, "SELECT pnl, pnl_source, exit_price FROM positions WHERE id = $1", r["position_id"])
    assert p["pnl_source"] == "local_calc" and p["exit_price"] == 100.0 and p["pnl"] == 0.0   # from the real fills


async def test_an_unconfirmed_exit_leaves_the_pnl_pending_instead_of_guessing(db):
    class _NoHistory(FakeBybit):
        async def get_order_history(self, **kw):
            placed = [c for n, c in self.calls if n == "place_market_order"]
            if placed and placed[-1].get("reduce_only"):
                return []                                              # the close's fill is not confirmed
            return await super().get_order_history(**kw)

    eng, _ = await _engine(db, _NoHistory(), dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    (p,) = await _rows(db, "SELECT pnl, pnl_source, exit_price, status FROM positions WHERE id = $1",
                       r["position_id"])
    assert (p["status"], p["pnl_source"], p["pnl"], p["exit_price"]) == ("CLOSED", "pending", None, None)


async def test_live_exit_is_a_reduce_only_sell(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    close = [kw for n, kw in fake.calls if n == "place_market_order"][-1]
    assert close["reduce_only"] is True and close["side"] == "Sell"


async def test_unprotected_position_is_closed_when_the_stop_cannot_be_set(db):
    fake = FakeBybit(stop_ok=False)
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    assert fake.names().count("set_trading_stop") == 4          # 3 attempts + 1 immediate recovery
    closes = [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]
    assert len(closes) == 1
    assert not r["success"] and r["reason"] == "stop_failed_position_closed"
    (p,) = await _rows(db, "SELECT status, pnl_source FROM positions")
    assert p["status"] == "CLOSED"                               # finished, not left CLOSING


async def test_unprotected_position_that_cannot_be_closed_is_reported_loudly(db):
    class _NoClose(FakeBybit):
        async def place_market_order(self, **kw):
            if kw.get("reduce_only"):
                self._rec("place_market_order", **kw)
                raise RuntimeError("Bybit API error: rate limited")
            return await super().place_market_order(**kw)

    fake = _NoClose(stop_ok=False)
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    assert not r["success"] and r["reason"] == "stop_failed_position_open_unprotected"
    assert r["close_error"]
    (p,) = await _rows(db, "SELECT status FROM positions")
    assert p["status"] == "OPEN"                                  # still visible to sync and the stop audit


async def test_db_failure_after_a_real_order_triggers_recovery_import(db, monkeypatch):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    imported = []

    async def boom(**kw):
        raise RuntimeError("disk full")

    async def fake_import():
        imported.append(True)
        return {"imported_count": 0}

    monkeypatch.setattr(eng, "_create_position_record", boom)
    monkeypatch.setattr(eng.position_tracker, "import_exchange_positions", fake_import)
    r = await eng.execute_entry(_sig())
    assert not r["success"] and r["reason"].startswith("db_write_failed_recovery_pending")
    assert imported == [True]


# ---- refusal gates (nothing is sent) --------------------------------------------------

@pytest.mark.parametrize("signal, reason", [
    (_sig(stop_price=None), "stop_required"),
    (_sig(stop_price=101.0), "stop_wrong_side"),
    (_sig(direction="short", stop_price=99.0), "stop_wrong_side"),
    (_sig(stop_price=99.8), "stop_too_close"),                     # 0.2% < 0.5% minimum
    (_sig(size={"qty": 0.001}), "qty_below_minimum"),              # rounds to 0 on the 0.01 step
    (_sig(size={"notional_usd": 10.5}), "qty_below_minimum"),     # price raised below: qty rounds to 0
])
async def test_entry_gates_refuse_before_any_order(db, signal, reason):
    fake = FakeBybit()
    if reason == "qty_below_minimum":
        fake.price = 2000.0                                        # 10.5/2000 = 0.00525 < 0.01 min qty
        signal["stop_price"] = 1900.0
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(signal)
    assert not r["success"] and r["reason"].startswith(reason)
    assert "place_market_order" not in fake.names()


async def test_stop_can_be_waived_only_by_the_deployment(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings={**SETTINGS, "require_stop": False})
    r = await eng.execute_entry(_sig(stop_price=None))
    assert r["success"]


async def test_second_entry_on_the_same_symbol_is_refused(db):
    eng, _ = await _engine(db, FakeBybit())
    assert (await eng.execute_entry(_sig()))["success"]
    r = await eng.execute_entry(_sig())
    assert r == {"success": False, "reason": "position_exists"}


async def test_risk_rejection_marks_the_signal_skipped(db):
    class _Log:
        def __init__(self):
            self.skipped = []

        async def mark_skipped(self, sid, reason):
            self.skipped.append((sid, reason))

    fake = FakeBybit(balance=50.0)                                 # below the minimum balance
    eng, _ = await _engine(db, fake, settings={**SETTINGS, "min_balance": 100.0})
    eng.signal_log = _Log()
    r = await eng.execute_entry(_sig(signal_id=7))
    assert not r["success"] and eng.signal_log.skipped[0][0] == 7


async def test_executed_signal_is_linked_atomically(db):
    eng, _ = await _engine(db, FakeBybit())
    async with db.session() as s:
        sig = Signal(source="api", signal_id="abc", status="accepted")
        s.add(sig)
        await s.commit()
    r = await eng.execute_entry(_sig(signal_id=sig.id))
    (row,) = await _rows(db, "SELECT status, result FROM signals")
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert row["status"] == "executed" and result["position_id"] == r["position_id"]


# ---- partial exit, fast exit, finalize ------------------------------------------------

async def test_partial_exit_reduces_size(db):
    eng, _ = await _engine(db, FakeBybit())
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    r = await eng.execute_partial_exit(pos, 40)
    assert r["success"] and r["partial_qty"] == 2.0 and r["remaining_qty"] == 3.0
    (p,) = await _rows(db, "SELECT size, status FROM positions")
    assert (p["size"], p["status"]) == (3.0, "OPEN")


async def test_fast_exit_then_finalize_paper_position(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    res = await eng.execute_exit_fast(pos)
    fake.price = 90.0
    (fin,) = await eng.finalize_closed_positions([res])
    assert fin["pnl_source"] == "dry_run" and fin["pnl"] == pytest.approx(-50.0)
    assert "get_closed_pnl" not in fake.names()


NO_LEVERAGE = {k: v for k, v in SETTINGS.items() if k != "leverage"}


async def test_no_leverage_is_invented_on_any_close_path(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings=NO_LEVERAGE)
    assert eng.position_tracker.default_leverage is None
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    res = await eng.execute_exit_fast(pos)
    await eng.finalize_closed_positions([res])
    async with db.acquire() as c:
        assert await c.fetchval("SELECT leverage FROM positions WHERE id = $1", pos["id"]) is None


async def test_exit_keeps_the_recorded_leverage(db):
    eng, _ = await _engine(db, FakeBybit(), settings=NO_LEVERAGE)
    r = await eng.execute_entry(_sig())
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET leverage = 3 WHERE id = $1", r["position_id"])
    (pos,) = await eng.position_tracker.get_all_positions()
    pos = {k: v for k, v in pos.items() if k != "leverage"}          # a caller without the field
    await eng.execute_exit(pos, reason="test")
    async with db.acquire() as c:
        assert await c.fetchval("SELECT leverage FROM positions WHERE id = $1", r["position_id"]) == 3


# ---- live feed (private WebSocket) ----------------------------------------------------

async def _insert(db, symbol, dry_run, deployment_id=None):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        return await c.fetchval(
            "INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, dry_run, opened_at,"
            " created_at, updated_at) VALUES ($1, $2, 'LONG', 1, 100, 'OPEN', $3, $4, $4, $4) RETURNING id",
            deployment_id, symbol, dry_run, now)


async def test_exchange_close_event_closes_the_real_row_never_the_paper_one(db):
    fake = FakeBybit(closed_pnl={"BTCUSDT": [{"avgEntryPrice": "100", "closedPnl": "3", "avgExitPrice": "103",
                                               "orderType": "Market"}]})
    eng, _ = await _engine(db, fake)
    real = await _insert(db, "BTCUSDT", False)
    paper = await _insert(db, "BTCUSDT", True)
    await eng._on_realtime_position_update(PositionUpdate.from_bybit_data({"symbol": "BTCUSDT", "size": "0"}))
    rows = {r["id"]: r for r in await _rows(db, "SELECT * FROM positions")}
    assert rows[real]["status"] == "CLOSED" and rows[real]["pnl_source"] == "pending"   # re-priced later
    assert rows[paper]["status"] == "OPEN"


async def test_unrealized_update_skips_paper_and_other_accounts(db):
    eng, _ = await _engine(db, FakeBybit())
    async with db.session() as s:
        other = Account(name="sub", credentials_ref="SUB_ACCOUNT_1")
        s.add(other)
        await s.flush()
        dep = Deployment(name="sub", account_id=other.id)
        s.add(dep)
        await s.commit()
    real = await _insert(db, "ETHUSDT", False)
    paper = await _insert(db, "ETHUSDT", True)
    keyed = await _insert(db, "ETHUSDT", False, dep.id)
    upd = PositionUpdate.from_bybit_data({"symbol": "ETHUSDT", "side": "Buy", "size": "1", "unrealisedPnl": "7"})
    await eng._on_realtime_position_update(upd)
    got = {r["id"]: r["unrealized_pnl"] for r in await _rows(db, "SELECT id, unrealized_pnl FROM positions")}
    assert got == {real: 7.0, paper: None, keyed: None}


# ---- stop audit ------------------------------------------------------------------------

_NAKED = {"symbol": "BTCUSDT", "side": "Buy", "size": "1", "avgPrice": "100", "markPrice": "101", "stopLoss": ""}


async def test_stop_audit_rearms_the_recorded_stop_when_live(db):
    fake = FakeBybit(positions=[_NAKED, {**_NAKED, "symbol": "ETHUSDT", "stopLoss": "90"}])
    eng, _ = await _engine(db, fake, dry_run=False)
    async with db.acquire() as c:
        now = datetime.now(UTC)
        await c.execute("INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, initial_stop_price,"
                        " created_at, updated_at) VALUES ('BTCUSDT', 'LONG', 1, 100, 'OPEN', $1, 95.04, $1, $1)", now)
    out = await eng.audit_stops()
    assert (out["checked"], out["naked"], out["rearmed"]) == (2, 1, 1)
    stop = dict(fake.calls)["set_trading_stop"]
    assert stop["symbol"] == "BTCUSDT" and stop["stop_loss"] == pytest.approx(95.1)


async def test_stop_audit_in_dry_run_sends_nothing(db):
    fake = FakeBybit(positions=[{**_NAKED, "size": "10"}])        # $1,000 position: 1% of 10k = a 10% stop
    eng, _ = await _engine(db, fake)
    from relay.safety.prop_rules import PropRules
    out = await eng.audit_stops(PropRules(initial_balance=10_000, max_position_risk_pct=1.0))
    assert out["would_rearm"] == 1 and out["rearmed"] == 0
    assert "set_trading_stop" not in fake.names()


async def test_stop_audit_reports_what_it_cannot_fix(db):
    eng, _ = await _engine(db, FakeBybit(positions=[_NAKED]), dry_run=False)
    out = await eng.audit_stops()
    assert out["still_naked"] == ["BTCUSDT"]


# ---- lifecycle ----------------------------------------------------------------------------

async def test_start_and_stop(db):
    eng, _ = await _engine(db, FakeBybit())
    await eng.start()
    assert eng._running and len(eng._tasks) == 3   # position sync, stop audit, stop rules
    await eng.stop()
    assert not eng._running and eng._tasks == []


async def test_entry_before_initialize_is_refused(db):
    eng = ExecutionEngine(db, FakeBybit(), SETTINGS)
    r = await eng.execute_entry(_sig())
    assert r == {"success": False, "reason": "execution_engine_not_initialized"}


async def test_settings_provider_may_be_an_async_callable(db):
    async def provider():
        return {**SETTINGS, "require_stop": False}
    eng = ExecutionEngine(db, FakeBybit(), provider)
    await eng.initialize()
    assert (await eng.get_execution_settings())["require_stop"] is False


# ---- fitting the size to the venue's lot rules -------------------------------------------

BTC_PERP_LOT = {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "1500", "maxMktOrderQty": "150",
                "minNotionalValue": "5"}   # Bybit BTCUSDT perpetual, as published


async def test_size_shrunk_by_the_lot_step_is_reported(db):
    """$100 of BTC at 77,676.5 on a 0.001-step contract can only be 0.001 (77.68): the result says so."""
    eng, _ = await _engine(db, FakeBybit(price=77676.5, lot=BTC_PERP_LOT))
    r = await eng.execute_entry(_sig(size={"notional_usd": 100}, stop_price=75500))
    assert r["success"] and r["quantity"] == 0.001
    z = r["sizing"]
    assert z["requested_notional"] == pytest.approx(100.0) and z["notional"] == pytest.approx(77.6765)
    assert z["shortfall_pct"] == pytest.approx(22.32, abs=0.01) and z["adjustment"] == "lot_step"


async def test_shortfall_above_the_users_limit_is_refused(db):
    fake = FakeBybit(price=77676.5, lot=BTC_PERP_LOT)
    eng, _ = await _engine(db, fake, settings={**SETTINGS, "max_size_shortfall_pct": 5})
    r = await eng.execute_entry(_sig(size={"notional_usd": 100}, stop_price=75500))
    assert not r["success"] and r["reason"] == "size_shortfall_exceeded"
    assert r["sizing"]["quantity"] == 0.001 and "place_market_order" not in fake.names()


async def test_shortfall_within_the_users_limit_is_placed(db):
    eng, _ = await _engine(db, FakeBybit(price=77676.5, lot=BTC_PERP_LOT),
                           settings={**SETTINGS, "max_size_shortfall_pct": 25})
    assert (await eng.execute_entry(_sig(size={"notional_usd": 100}, stop_price=75500)))["success"]


async def test_finer_venue_step_gets_closer(db):
    """Hyperliquid-style 5-decimal step: the same $100 lands within 1%."""
    lot = {"qtyStep": "0.00001", "minOrderQty": "0.00001", "minNotionalValue": "10"}
    eng, _ = await _engine(db, FakeBybit(price=77676.5, lot=lot))
    r = await eng.execute_entry(_sig(size={"notional_usd": 100}, stop_price=75500))
    assert r["quantity"] == pytest.approx(0.00128) and r["sizing"]["shortfall_pct"] < 1


async def test_unknown_lot_rules_send_nothing(db):
    fake = FakeBybit(lot={})                                  # venue published no step
    eng, _ = await _engine(db, fake)
    r = await eng.execute_entry(_sig())
    assert not r["success"] and r["reason"] == "instrument_rules_unavailable"


async def test_below_the_venue_minimum_order_value_is_refused(db):
    eng, _ = await _engine(db, FakeBybit(price=77676.5, lot=BTC_PERP_LOT))
    r = await eng.execute_entry(_sig(size={"qty": 0.00005}, stop_price=75500))   # rounds to 0
    assert not r["success"] and r["reason"] == "qty_below_minimum"


async def test_below_the_venue_minimum_order_value_is_refused_after_rounding(db):
    lot = {**BTC_PERP_LOT, "qtyStep": "0.00001", "minOrderQty": "0.00001"}
    fake = FakeBybit(price=77676.5, lot=lot)
    eng, _ = await _engine(db, fake)
    r = await eng.execute_entry(_sig(size={"qty": 0.00005}, stop_price=75500))   # $3.88 < the venue's $5
    assert not r["success"] and r["reason"] == "below_min_notional"
    assert "place_market_order" not in fake.names()


async def test_market_orders_use_the_market_quantity_cap(db):
    eng, _ = await _engine(db, FakeBybit(price=77676.5, balance=1e9, lot=BTC_PERP_LOT),
                           settings={**SETTINGS, "max_exposure_pct": None})
    r = await eng.execute_entry(_sig(size={"qty": 200}, stop_price=75500))
    assert r["success"] and r["quantity"] == 150
    assert r["sizing"]["adjustment"] == "venue_max_qty" and r["sizing"]["shortfall_pct"] == pytest.approx(25)


async def test_reduce_below_the_venue_step_is_explained(db):
    eng, _ = await _engine(db, FakeBybit(price=77676.5, lot=BTC_PERP_LOT))
    r = await eng.execute_entry(_sig(size={"notional_usd": 100}, stop_price=75500))
    pos = (await eng._own_open_positions())[0]
    half = await eng.execute_partial_exit(pos, 50, reason="test")         # 0.0005 BTC on a 0.001 step
    assert not half["success"] and "below the venue minimum 0.001" in half["error"]
    assert half["sizing"]["requested_quantity"] == pytest.approx(0.0005)
    assert r["success"]


# ---- background loops survive errors ------------------------------------------------------

async def test_sync_loop_survives_an_error_with_braces_and_uses_the_configured_interval(db, monkeypatch):
    """Exchange errors carry JSON ({...}); logging one must not kill the loop. The interval comes
    from the settings."""
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "position_sync_interval_s": 7})
    calls, sleeps = [], []

    async def _boom():
        calls.append(1)
        if len(calls) >= 3:
            eng._running = False
        raise RuntimeError('HTTP 400: {"retCode":10001,"retMsg":"bad"}')

    async def _sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(eng.position_tracker, "import_exchange_positions", _boom)
    monkeypatch.setattr(engine_mod.asyncio, "sleep", _sleep)
    eng._running = True
    await eng._position_sync_loop()
    assert len(calls) == 3 and sleeps == [7, 7, 7]


async def test_an_emergency_stop_during_entry_preparation_wins(db):
    """The halt is checked again right before the first exchange write."""
    class _Racing(FakeBybit):
        async def get_ticker(self, symbol, category="linear"):
            eng.halted_reason = "emergency_stop"          # fires while the entry is being prepared
            return await super().get_ticker(symbol, category)

    fake = _Racing()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    assert r == {"success": False, "reason": "engine_halted"}
    assert not {"place_market_order", "set_leverage", "switch_isolated_margin"} & set(fake.names())


# ---- stops and take-profits are never invented -------------------------------------------

async def test_a_fill_past_the_stop_closes_the_position_instead_of_moving_the_stop(db):
    fake = FakeBybit(price=94.0)                                   # expected 100, filled at 94
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(entry_price=100.0, stop_price=95.0))
    assert not r["success"] and r["reason"] == "stop_breached_at_fill_position_closed"
    assert "set_trading_stop" not in fake.names()                  # no stop invented at the market
    assert [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]


async def test_a_stop_beyond_the_users_maximum_is_refused_before_trading(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings={**SETTINGS, "max_stop_distance_pct": 3})
    r = await eng.execute_entry(_sig(stop_price=90.0))             # 10% away
    assert r == {"success": False, "reason": "stop_too_far"} and "place_market_order" not in fake.names()


async def test_a_take_profit_on_the_wrong_side_is_refused_before_trading(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    r = await eng.execute_entry(_sig(take_profit_price=99.0))      # long, TP below entry
    assert r == {"success": False, "reason": "take_profit_wrong_side"}


async def test_a_take_profit_already_reached_at_fill_is_dropped_and_reported(db):
    fake = FakeBybit(price=111.0)                                  # expected 100, filled at 111
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig(entry_price=100.0, stop_price=95.0, take_profit_price=110.0))
    assert r["success"] and r["take_profit_price"] is None
    assert r["take_profit_note"].startswith("take_profit_already_reached_at_fill")
    assert all("take_profit" not in kw for n, kw in fake.calls if n == "set_trading_stop")


async def test_a_take_profit_without_a_stop_is_still_set(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "require_stop": False})
    r = await eng.execute_entry(_sig(stop_price=None, take_profit_price=110.0))
    assert r["success"] and r["take_profit_note"] is None
    (tp,) = [kw for n, kw in fake.calls if n == "set_trading_stop"]
    assert tp["take_profit"] == 110.0 and "stop_loss" not in tp


async def test_stop_audit_reports_a_breached_stop_instead_of_inventing_one(db):
    naked_underwater = {**_NAKED, "markPrice": "90"}               # recorded stop 95, price now 90
    fake = FakeBybit(positions=[naked_underwater])
    eng, _ = await _engine(db, fake, dry_run=False)
    async with db.acquire() as c:
        now = datetime.now(UTC)
        await c.execute("INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, initial_stop_price,"
                        " created_at, updated_at) VALUES ('BTCUSDT', 'LONG', 1, 100, 'OPEN', $1, 95.0, $1, $1)", now)
    out = await eng.audit_stops()
    assert out["breached"] == ["BTCUSDT"] and out["rearmed"] == 0
    assert "set_trading_stop" not in fake.names()


async def test_stop_audit_never_borrows_another_accounts_stop(db):
    fake = FakeBybit(positions=[_NAKED])
    eng, _ = await _engine(db, fake, dry_run=False)
    async with db.session() as s:
        acc = Account(name="other", credentials_ref="OTHER_ACCOUNT")
        s.add(acc)
        await s.flush()
        dep = Deployment(name="other", account_id=acc.id)
        s.add(dep)
        await s.commit()
        other = dep.id
    async with db.acquire() as c:
        now = datetime.now(UTC)
        await c.execute("INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, opened_at,"
                        " initial_stop_price, created_at, updated_at)"
                        " VALUES ($1, 'BTCUSDT', 'LONG', 1, 100, 'OPEN', $2, 97.0, $2, $2)", other, now)
    out = await eng.audit_stops()
    assert out["still_naked"] == ["BTCUSDT"] and "set_trading_stop" not in fake.names()


async def test_closes_follow_the_positions_own_mode_not_the_current_flag(db):
    """Paper row + relay now live -> simulated close; real row + relay now in dry-run -> real close."""
    flag = {"dry": True}
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=lambda: flag["dry"])
    paper = await eng.execute_entry(_sig())                       # opened in dry-run: a paper row
    flag["dry"] = False
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos, reason="test")
    assert "place_market_order" not in fake.names()               # the paper position is not closed for real
    assert paper["dry_run"] is True

    real = await eng.execute_entry(_sig(symbol="ETHUSDT"))        # opened live: a real row
    flag["dry"] = True
    (pos,) = [p for p in await eng.position_tracker.get_all_positions() if p["id"] == real["position_id"]]
    await eng.execute_exit(pos, reason="test")
    closes = [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]
    assert len(closes) == 1 and closes[0]["symbol"] == "ETHUSDT"  # the real position IS closed for real


async def test_a_halted_engine_refuses_before_touching_the_exchange(db):
    class _Watch(FakeBybit):
        async def get_ticker(self, symbol, category="linear"):
            self._rec("get_ticker", symbol=symbol)
            return await super().get_ticker(symbol, category)

    fake = _Watch()
    eng, _ = await _engine(db, fake)
    eng.halted_reason = "emergency_stop"
    assert (await eng.execute_entry(_sig()))["reason"] == "engine_halted"
    assert fake.calls == []


async def test_the_stop_is_checked_after_tick_alignment(db):
    """99.95 is below a 100.0 fill, but aligned to the 0.1 tick toward entry it becomes 100.0:
    already breached, so the position is closed instead of sending a stop that fires at once."""
    fake = FakeBybit(price=100.0)
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "min_stop_distance_pct": None})
    r = await eng.execute_entry(_sig(stop_price=99.95))
    assert r["reason"] == "stop_breached_at_fill_position_closed"
    assert "set_trading_stop" not in fake.names()


# ---- the recorded entry is the venue's fill ------------------------------------------------

async def _entry_row(db, pid):
    (row,) = await _rows(db, f"SELECT entry_price, size, entry_meta FROM positions WHERE id = {pid}")
    meta = row["entry_meta"]
    return row, (json.loads(meta) if isinstance(meta, str) else meta)


async def test_the_recorded_entry_is_the_venues_average_price_not_the_expected_one(db):
    class _Slipped(FakeBybit):
        async def get_order_history(self, **kw):
            return [{**(await super().get_order_history(**kw))[0], "avgPrice": "100.4"}]

    eng, _ = await _engine(db, _Slipped(), dry_run=False)
    r = await eng.execute_entry(_sig())
    row, meta = await _entry_row(db, r["position_id"])
    assert row["entry_price"] == 100.4 and r["fill_price"] == 100.4
    assert meta["entry_price_source"] == "venue_order"


async def test_a_partial_fill_records_the_quantity_that_filled(db):
    class _Partial(FakeBybit):
        async def get_order_history(self, **kw):
            return [{"orderStatus": "PartiallyFilledCanceled", "avgPrice": "100", "cumExecQty": "2", "orderId": "o1"}]

    eng, _ = await _engine(db, _Partial(), dry_run=False)
    r = await eng.execute_entry(_sig())                            # asks for 5
    row, _ = await _entry_row(db, r["position_id"])
    assert row["size"] == 2.0 and r["quantity"] == 2.0 and r["sizing"]["filled_quantity"] == 2.0


async def test_an_unconfirmed_fill_uses_the_venue_position_when_it_is_ours_alone(db):
    class _Slow(FakeBybit):
        async def get_order_history(self, **kw):
            return []

        async def get_positions(self, category="linear", symbol=None, settle_coin=None):
            return [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100.3"}]

    eng, _ = await _engine(db, _Slow(), dry_run=False)
    r = await eng.execute_entry(_sig())
    row, meta = await _entry_row(db, r["position_id"])
    assert row["entry_price"] == 100.3 and meta["entry_price_source"] == "venue_position"


async def test_an_unconfirmed_fill_without_a_clean_venue_position_is_marked_as_expected(db):
    class _Slow(FakeBybit):
        async def get_order_history(self, **kw):
            return []

        async def get_positions(self, category="linear", symbol=None, settle_coin=None):
            return [{"symbol": "BTCUSDT", "side": "Buy", "size": "8", "avgPrice": "97"}]   # not ours alone

    eng, _ = await _engine(db, _Slow(), dry_run=False)
    r = await eng.execute_entry(_sig())
    row, meta = await _entry_row(db, r["position_id"])
    assert row["entry_price"] == 100.0 and meta["entry_price_source"] == "expected_unconfirmed"


# ---- leverage and margin mode are applied exactly, or the entry does not happen ------------

async def test_a_rejected_leverage_refuses_the_entry(db):
    class _Rejects(FakeBybit):
        async def set_leverage(self, **kw):
            self._rec("set_leverage", **kw)
            return {"retCode": 110044, "retMsg": "risk limit"}

    fake = _Rejects()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    assert not r["success"] and r["reason"].startswith("leverage_not_set")
    assert "place_market_order" not in fake.names()


async def test_leverage_is_sent_exactly_and_margin_mode_is_left_alone_unless_set(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "leverage": 2.5})
    await eng.execute_entry(_sig())
    assert dict(fake.calls)["set_leverage"]["leverage"] == 2.5
    assert "switch_isolated_margin" not in fake.names()


async def test_a_chosen_margin_mode_is_applied(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "isolated_margin": True})
    await eng.execute_entry(_sig())
    assert dict(fake.calls)["switch_isolated_margin"]["isolated"] is True


# ---- risk rails at the engine ------------------------------------------------------------

async def test_cooldown_after_a_close_is_enforced_and_survives_a_restart(db):
    fake = FakeBybit()
    settings = {**SETTINGS, "cooldown_hours": 4}
    eng, dep = await _engine(db, fake, settings=settings)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos, reason="test")
    assert (await eng.execute_entry(_sig()))["reason"] == "cooldown"
    fresh = ExecutionEngine(db, fake, settings, dry_run=True, deployment_id=dep)        # a restart
    await fresh.initialize()
    assert (await fresh.execute_entry(_sig()))["reason"] == "cooldown"
    assert r["success"]


async def test_the_new_order_itself_counts_toward_the_exposure_limit(db):
    """10,000 equity, 30% limit, nothing open: a 5,000 order (50%) is refused before it is sent."""
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, settings={**SETTINGS, "max_exposure_pct": 30})
    r = await eng.execute_entry(_sig(size={"notional_usd": 5_000}))
    assert not r["success"] and "Exposure limit" in r["reason"]
    assert "place_market_order" not in fake.names()
    assert (await eng.execute_entry(_sig(size={"notional_usd": 2_000})))["success"]


async def test_simultaneous_signals_cannot_both_pass_a_position_limit(db):
    import asyncio
    eng, _ = await _engine(db, FakeBybit(), settings={**SETTINGS, "max_positions": 1})
    a, b = await asyncio.gather(eng.execute_entry(_sig(symbol="BTCUSDT")),
                                eng.execute_entry(_sig(symbol="ETHUSDT")))
    assert sorted([a["success"], b["success"]]) == [False, True]


async def test_live_limits_count_the_whole_account_but_not_other_accounts(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False, settings={**SETTINGS, "max_positions": 1})
    now = datetime.now(UTC)
    async with db.session() as s:
        other = Account(name="other", credentials_ref="OTHER_ACCOUNT")
        s.add(other)
        await s.flush()
        d = Deployment(name="other", account_id=other.id)
        s.add(d)
        await s.commit()
        other_dep = d.id
    async with db.acquire() as c:                                 # a position on ANOTHER account
        await c.execute("INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, opened_at,"
                        " created_at, updated_at) VALUES ($1, 'XUSDT', 'LONG', 1, 1, 'OPEN', $2, $2, $2)", other_dep, now)
    assert (await eng.execute_entry(_sig()))["success"]
    async with db.acquire() as c:                                 # a hand-opened position on THIS account
        await c.execute("UPDATE positions SET status = 'CLOSED' WHERE symbol = 'BTCUSDT'")
        await c.execute("INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, opened_at,"
                        " created_at, updated_at) VALUES (NULL, 'YUSDT', 'LONG', 1, 1, 'OPEN', $1, $1, $1)", now)
    r = await eng.execute_entry(_sig(symbol="ETHUSDT"))
    assert not r["success"] and "Position limit reached: 1/1" in r["reason"]


# ---- partial closes are recorded and counted ----------------------------------------------

async def test_a_partial_close_is_recorded_and_counted_in_the_final_result(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)                                # paper: priced from simulated fills
    r = await eng.execute_entry(_sig())                            # 5 @ 100
    (pos,) = await eng.position_tracker.get_all_positions()
    fake.price = 102.0
    red = await eng.execute_partial_exit(pos, 40)                  # 2 @ 102 -> +4
    assert red["partial_pnl"] == pytest.approx(4.0) and red["remaining_qty"] == pytest.approx(3.0)
    fake.price = 104.0
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)                                    # 3 @ 104 -> +12
    (p,) = await _rows(db, "SELECT pnl, pnl_percent FROM positions WHERE id = $1", r["position_id"])
    assert p["pnl"] == pytest.approx(16.0)                         # the whole trade, not just the rest
    assert p["pnl_percent"] == pytest.approx(16 / 500 * 100)       # of the ORIGINAL position value


async def test_concurrent_reduces_work_from_the_current_size(db):
    import asyncio
    eng, _ = await _engine(db, FakeBybit())
    await eng.execute_entry(_sig(size={"qty": 10}))
    (pos,) = await eng.position_tracker.get_all_positions()
    a, b = await asyncio.gather(eng.execute_partial_exit(pos, 50), eng.execute_partial_exit(pos, 50))
    assert {a["remaining_qty"], b["remaining_qty"]} == {5.0, 2.5}  # 50% of 10, then 50% of 5
    (p,) = await _rows(db, "SELECT size FROM positions WHERE id = $1", pos["id"])
    assert p["size"] == pytest.approx(2.5)


async def test_a_reduce_of_everything_is_a_full_close(db):
    eng, _ = await _engine(db, FakeBybit())
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    res = await eng.execute_partial_exit(pos, 100)
    assert res["closed_fully"]
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "CLOSED"


async def test_a_venue_close_message_never_overwrites_a_recorded_close(db):
    from relay.execution.close_record import write_close
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    async with db.acquire() as c:
        wrote = await write_close(c, pos, {"exit_price": 1.0, "pnl": -999.0, "pnl_percent": None,
                                           "pnl_source": "pending", "close_order_id": None},
                                  datetime.now(UTC), "closed_on_venue")
    (p,) = await _rows(db, "SELECT pnl FROM positions WHERE id = $1", r["position_id"])
    assert wrote is False and p["pnl"] != -999.0


async def test_a_paper_close_without_a_price_is_refused_and_the_position_stays_open(db):
    class _NoTicker(FakeBybit):
        async def get_ticker(self, symbol, category="linear"):
            if self.calls and any(n == "place_market_order" for n, _ in self.calls) or getattr(self, "closing", False):
                raise RuntimeError("ticker unavailable")
            return await super().get_ticker(symbol, category)

    fake = _NoTicker()
    eng, _ = await _engine(db, fake)
    r = await eng.execute_entry(_sig())                            # paper entry reads the ticker
    fake.closing = True
    (pos,) = await eng.position_tracker.get_all_positions()
    with pytest.raises(Exception, match="no market price"):
        await eng.execute_exit(pos)                                # not a -100% loss, not an unknown result
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "OPEN"                                   # retry the close once a price is back


async def test_a_close_seen_on_the_live_feed_does_not_guess_its_cause(db):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    r = await eng.execute_entry(_sig())
    await eng._handle_realtime_position_closed("BTCUSDT", None)
    (t,) = await _rows(db, "SELECT exit_reason FROM trades WHERE position_id = $1 AND trade_type = 'exit'",
                       r["position_id"])
    assert t["exit_reason"] == "closed_on_venue"


async def test_instrument_rules_are_refreshed_after_the_cache_period(db, monkeypatch):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake)
    calls = []
    original = fake.list_instruments

    async def _counting(**kw):
        calls.append(1)
        return await original(**kw)

    fake.list_instruments = _counting
    await eng._instrument_specs("BTCUSDT")
    await eng._instrument_specs("BTCUSDT")
    assert len(calls) == 1
    monkeypatch.setattr(engine_mod, "INSTRUMENT_CACHE_S", 0)
    await eng._instrument_specs("BTCUSDT")
    assert len(calls) == 2


# ---- round 2: the relay's own close vs the background closers ------------------------------

class _FeedRacer(FakeBybit):
    """The live feed reports the venue position gone the moment the close order is sent."""
    eng = None

    async def place_market_order(self, **kw):
        import asyncio
        r = await super().place_market_order(**kw)
        if kw.get("reduce_only") and self.eng:
            self.positions = []
            asyncio.get_running_loop().create_task(self.eng._handle_realtime_position_closed(kw["symbol"], None))
            await _real_sleep(0.05)
        return r


async def _exit_trades(db, pid):
    return [r["exit_reason"] for r in await _rows(
        db, "SELECT exit_reason FROM trades WHERE position_id = $1 AND trade_type = 'exit'", pid)]


async def test_a_relay_close_racing_the_live_feed_records_one_exit(db):
    fake = _FeedRacer()
    eng, _ = await _engine(db, fake, dry_run=False)
    fake.eng = eng
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos, reason="api")
    assert await _exit_trades(db, r["position_id"]) == ["api"]


async def test_a_fast_close_racing_the_live_feed_never_reopens_or_double_closes(db):
    fake = _FeedRacer()
    eng, _ = await _engine(db, fake, dry_run=False)
    fake.eng = eng
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    res = await eng.execute_exit_fast(pos)
    await eng.finalize_closed_positions([res])
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "CLOSED" and await _exit_trades(db, r["position_id"]) == ["close_all"]


async def test_the_position_sync_leaves_a_position_the_relay_is_closing(db):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    r = await eng.execute_entry(_sig())
    eng._own_close(r["position_id"])                               # the relay is mid-close
    await eng.position_tracker.sync_all_positions()                # venue shows nothing open
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "OPEN"


class _PartialClose(FakeBybit):
    async def get_order_history(self, **kw):
        placed = [c for n, c in self.calls if n == "place_market_order"]
        if placed and placed[-1].get("reduce_only"):
            return [{"orderStatus": "PartiallyFilledCanceled", "avgPrice": "101", "cumExecQty": "3",
                     "orderId": "c1"}]
        return await super().get_order_history(**kw)


async def test_a_partly_filled_close_keeps_the_rest_open_and_says_so(db):
    eng, _ = await _engine(db, _PartialClose(), dry_run=False)
    r = await eng.execute_entry(_sig())                            # 5
    (pos,) = await eng.position_tracker.get_all_positions()
    with pytest.raises(Exception, match="still open"):
        await eng.execute_exit(pos)
    (p,) = await _rows(db, "SELECT status, size FROM positions WHERE id = $1", r["position_id"])
    assert (p["status"], p["size"]) == ("OPEN", 2.0)


async def test_a_partly_filled_fast_close_reopens_the_rest(db):
    eng, _ = await _engine(db, _PartialClose(), dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    (fin,) = await eng.finalize_closed_positions([await eng.execute_exit_fast(pos)])
    assert fin["partial"] and fin["remaining_qty"] == 2.0
    (p,) = await _rows(db, "SELECT status, size FROM positions WHERE id = $1", r["position_id"])
    assert (p["status"], p["size"]) == ("OPEN", 2.0)


async def test_a_close_and_a_reduce_at_once_work_from_one_consistent_size(db):
    import asyncio
    eng, _ = await _engine(db, FakeBybit())
    r = await eng.execute_entry(_sig(size={"qty": 10}))
    (pos,) = await eng.position_tracker.get_all_positions()
    await asyncio.gather(eng.execute_partial_exit(pos, 50), eng.execute_exit(pos))
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    closed = await _rows(db, "SELECT sum(qty) AS q FROM trades WHERE position_id = $1 AND trade_type IN "
                             "('exit', 'partial_exit')", r["position_id"])
    assert p["status"] == "CLOSED" and closed[0]["q"] == pytest.approx(10.0)   # never more than held


async def test_a_reduce_of_everything_that_fails_is_reported_not_raised(db):
    class _Refuses(FakeBybit):
        async def place_market_order(self, **kw):
            if kw.get("reduce_only"):
                raise RuntimeError("Bybit API error 10001: position idx not match")
            return await super().place_market_order(**kw)

    eng, _ = await _engine(db, _Refuses(), dry_run=False)
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    res = await eng.execute_partial_exit(pos, 100)
    assert res["success"] is False and res["error"]


async def test_a_paper_close_left_closing_is_finished_by_the_sweep(db):
    eng, _ = await _engine(db, FakeBybit())
    r = await eng.execute_entry(_sig())
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING', updated_at = $1 WHERE id = $2",
                        datetime.now(UTC) - __import__("datetime").timedelta(minutes=5), r["position_id"])
    await eng._finish_stale_paper_closes()
    (p,) = await _rows(db, "SELECT status, pnl_source FROM positions WHERE id = $1", r["position_id"])
    assert (p["status"], p["pnl_source"]) == ("CLOSED", "dry_run")


async def test_the_cooldown_of_one_mode_does_not_block_the_other(db):
    flag = {"dry": True}
    eng, _ = await _engine(db, FakeBybit(), dry_run=lambda: flag["dry"],
                           settings={**SETTINGS, "cooldown_hours": 4})
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)                                    # a PAPER close
    flag["dry"] = False
    assert (await eng.execute_entry(_sig()))["success"]            # the live entry is not cooled down


async def test_a_paper_stop_is_never_cancelled_on_the_venue(db):
    from relay.execution.stop_loss_manager import StopLossManager
    fake = FakeBybit()
    calls = []

    async def _cancel(**kw):
        calls.append(kw)

    fake.cancel_order = _cancel
    assert await StopLossManager(fake, dry_run=False).cancel_stop_loss("dry_run_stop_BTCUSDT", "BTCUSDT")
    assert calls == []


async def test_close_all_closes_through_an_open_breaker_without_waiting(db):
    from relay.util.circuit_breaker import CircuitState

    class _Breaker:
        state = CircuitState.OPEN

    fake = FakeBybit()
    fake.is_circuit_open = lambda: True

    async def _never(**kw):
        raise AssertionError("close_all must not wait for the breaker")

    fake.wait_for_circuit_recovery = _never
    eng, _ = await _engine(db, fake, dry_run=False)
    await eng.execute_entry(_sig())
    res = await eng.close_all()
    assert res["success"] and res["positions_closed"] == 1


async def test_an_emergency_stop_closes_both_modes_and_refuses_an_entry_still_preparing(db):
    import asyncio
    flag = {"dry": True}
    gate, preparing = asyncio.Event(), asyncio.Event()

    class _Slow(FakeBybit):
        async def get_ticker(self, symbol, category="linear"):
            if symbol == "SOLUSDT":
                preparing.set()
                await gate.wait()                                  # an entry still being prepared
            return await super().get_ticker(symbol, category)

    eng, _ = await _engine(db, _Slow(), dry_run=lambda: flag["dry"])
    await eng.execute_entry(_sig())                                # paper BTC
    flag["dry"] = False
    await eng.execute_entry(_sig(symbol="ETHUSDT"))                # live ETH
    in_flight = asyncio.create_task(eng.execute_entry(_sig(symbol="SOLUSDT")))
    await asyncio.wait_for(preparing.wait(), 5)
    stop = asyncio.create_task(eng.close_all(reason="emergency_stop", stop_engine=True))
    while not eng.halted_reason:                                   # the halt is in place first
        await _real_sleep(0.001)
    gate.set()
    res, entry = await asyncio.gather(stop, in_flight)
    open_rows = await _rows(db, "SELECT symbol FROM positions WHERE status IN ('OPEN', 'CLOSING')")
    # The entry was still preparing (before its last halt check): it is refused, and both modes'
    # existing positions are closed.
    assert entry["reason"] == "engine_halted"
    assert open_rows == [] and res["positions_closed"] == 2


async def test_a_close_uses_the_current_size_not_the_callers_copy(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    await eng.execute_entry(_sig(size={"qty": 10}))
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_partial_exit(pos, 50)                        # DB size is now 5
    await eng.execute_exit(pos)                                    # pos still says 10
    closes = [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]
    assert [c["qty"] for c in closes] == ["5.0", "5.0"]


async def test_an_emergency_stop_includes_an_entry_that_was_already_placing_its_order(db):
    import asyncio
    gate, sending = asyncio.Event(), asyncio.Event()

    class _SlowOrder(FakeBybit):
        async def place_market_order(self, **kw):
            if not kw.get("reduce_only"):
                sending.set()
                await gate.wait()                                  # past the last halt check
            return await super().place_market_order(**kw)

    eng, _ = await _engine(db, _SlowOrder(), dry_run=False)
    entry = asyncio.create_task(eng.execute_entry(_sig()))
    # Wait until the order is really being sent (a fixed sleep raced the pre-order checks).
    await asyncio.wait_for(sending.wait(), 5)
    stop = asyncio.create_task(eng.close_all(reason="emergency_stop", stop_engine=True))
    while not eng.halted_reason:                                   # the stop is waiting for the entry
        await _real_sleep(0.001)
    gate.set()
    res, e = await asyncio.gather(stop, entry)
    assert e["success"] and res["positions_closed"] == 1
    assert await _rows(db, "SELECT id FROM positions WHERE status IN ('OPEN', 'CLOSING')") == []


async def test_a_close_of_an_unconfirmed_entry_stays_pending(db):
    class _Unconfirmed(FakeBybit):
        async def get_order_history(self, **kw):
            placed = [c for n, c in self.calls if n == "place_market_order"]
            if placed and not placed[-1].get("reduce_only"):
                return []                                          # the entry fill is never confirmed
            return await super().get_order_history(**kw)

    eng, _ = await _engine(db, _Unconfirmed(), dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    (p,) = await _rows(db, "SELECT pnl_source, pnl FROM positions WHERE id = $1", r["position_id"])
    assert (p["pnl_source"], p["pnl"]) == ("pending", None)        # no local PnL from a guessed entry


async def test_a_mode_switch_mid_entry_cannot_leave_a_real_position_without_a_real_stop(db):
    flag = {"dry": False}

    class _Flips(FakeBybit):
        async def get_order_history(self, **kw):
            flag["dry"] = True                                     # switched to dry-run after the real order
            return await super().get_order_history(**kw)

    fake = _Flips()
    eng, _ = await _engine(db, fake, dry_run=lambda: flag["dry"])
    r = await eng.execute_entry(_sig())
    assert r["success"] and r["dry_run"] is False
    assert "set_trading_stop" in fake.names()                      # the stop went to the venue
    (p,) = await _rows(db, "SELECT dry_run FROM positions WHERE id = $1", r["position_id"])
    assert not p["dry_run"]


async def test_a_switch_to_live_mid_entry_cannot_turn_a_paper_entry_into_a_real_order(db):
    flag = {"dry": True}

    class _Flips(FakeBybit):
        async def get_ticker(self, symbol, category="linear"):
            flag["dry"] = False                                    # switched to live while preparing
            return await super().get_ticker(symbol, category)

    fake = _Flips()
    eng, _ = await _engine(db, fake, dry_run=lambda: flag["dry"])
    r = await eng.execute_entry(_sig())
    assert r["success"] and r["dry_run"] is True
    assert "place_market_order" not in fake.names()


# ---- round 3 -----------------------------------------------------------------------------

class _CloseOutcome(FakeBybit):
    """The reduce-only close's venue record: configurable."""
    close_record = None

    async def get_order_history(self, **kw):
        placed = [c for n, c in self.calls if n == "place_market_order"]
        if placed and placed[-1].get("reduce_only"):
            return [] if self.close_record is None else [self.close_record]
        return await super().get_order_history(**kw)


async def test_a_fast_close_that_filled_nothing_keeps_the_position_open(db):
    fake = _CloseOutcome()
    fake.close_record = {"orderStatus": "Cancelled", "avgPrice": "", "cumExecQty": "0", "orderId": "c1"}
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "5", "avgPrice": "100", "unrealisedPnl": "0"}]
    res = await eng.close_all()                                    # the venue still holds the position
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "OPEN" and not res["success"] and "filled nothing" in res["errors"][0]["error"]


async def test_an_unconfirmed_fast_close_is_left_for_the_sync_to_settle(db):
    fake = _CloseOutcome()                                         # no record of the close at all
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    (fin,) = await eng.finalize_closed_positions([await eng.execute_exit_fast(pos)])
    assert fin["unconfirmed"] and not eng._close_owned(r["position_id"])
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "CLOSING"
    await eng.position_tracker.sync_all_positions()                # venue shows nothing open
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert p["status"] == "CLOSED"


async def test_a_close_never_sends_a_second_order_for_a_position_already_being_closed(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    res = await eng.execute_exit_fast(pos)                         # sent, not finalised yet
    await eng.execute_exit(pos)                                    # e.g. an emergency stop right after
    closes = [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]
    assert len(closes) == 1 and eng._close_owned(r["position_id"])
    await eng.finalize_closed_positions([res])
    assert not eng._close_owned(r["position_id"])


async def test_a_cancelled_close_releases_its_ownership(db):
    import asyncio

    class _Hangs(FakeBybit):
        async def place_market_order(self, **kw):
            if kw.get("reduce_only"):
                await asyncio.sleep(3600)
            return await super().place_market_order(**kw)

    eng, _ = await _engine(db, _Hangs(), dry_run=False)
    r = await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    task = asyncio.create_task(eng.execute_exit_fast(pos))
    await _real_sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not eng._close_owned(r["position_id"])


async def test_stale_close_ownership_is_ignored(db, monkeypatch):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    eng._closing[42] = engine_mod._time.monotonic() - engine_mod.CLOSE_OWNERSHIP_S - 1   # never released
    assert not eng._close_owned(42)


async def test_close_all_waits_for_another_close_and_then_finishes_the_job(db):
    import asyncio
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    r = await eng.execute_entry(_sig())
    eng._own_close(r["position_id"])                               # another request is closing it ...

    async def _other_close_gives_up():
        await _real_sleep(0.05)
        eng._release_close(r["position_id"])                       # ... and gives up without closing
    helper = asyncio.create_task(_other_close_gives_up())
    res = await eng.close_all()
    await helper
    (p,) = await _rows(db, "SELECT status FROM positions WHERE id = $1", r["position_id"])
    assert res["success"] and res["positions_closed"] == 1 and p["status"] == "CLOSED"


async def test_an_emergency_stop_never_counts_a_position_another_close_still_holds(db, monkeypatch):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    r = await eng.execute_entry(_sig())
    eng._own_close(r["position_id"])                               # held for good by another close
    original = eng._wait_close_released

    async def _short(pid, timeout=30.0):
        return await original(pid, timeout=0.05)

    monkeypatch.setattr(eng, "_wait_close_released", _short)
    res = await eng.close_all(reason="emergency_stop", stop_engine=True)
    assert not res["success"] and res["positions_closed"] == 0
    assert "another request" in res["errors"][0]["error"]


async def test_reclosing_an_unconfirmed_close_asks_the_venue_first(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", r["position_id"])
    (pos,) = await eng.position_tracker.get_all_positions()
    fake.positions = []                                            # the earlier close DID fill
    assert await eng.execute_exit(pos) == "closed"
    assert not [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]

    r2 = await eng.execute_entry(_sig(symbol="ETHUSDT"))
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", r2["position_id"])
    (pos2,) = [p for p in await eng.position_tracker.get_all_positions() if p["id"] == r2["position_id"]]
    fake.positions = [{"symbol": "ETHUSDT", "side": "Buy", "size": "2", "avgPrice": "100"}]   # 2 of 5 left
    await eng.execute_exit(pos2)
    (close,) = [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]
    assert close["qty"] == "2.0"                                   # only what is still open


async def test_an_unconfirmed_safety_close_is_not_reported_as_closed(db):
    class _NoCloseRecord(FakeBybit):
        async def get_order_history(self, **kw):
            placed = [c for n, c in self.calls if n == "place_market_order"]
            if placed and placed[-1].get("reduce_only"):
                return []
            return await super().get_order_history(**kw)

    eng, _ = await _engine(db, _NoCloseRecord(stop_ok=False), dry_run=False)
    r = await eng.execute_entry(_sig())
    assert r["reason"] == "stop_failed_close_unconfirmed"


async def test_a_separate_stop_is_kept_when_the_close_filled_nothing(db):
    fake = _CloseOutcome()
    fake.close_record = {"orderStatus": "Cancelled", "avgPrice": "", "cumExecQty": "0", "orderId": "c1"}
    eng, _ = await _engine(db, fake, dry_run=False)
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    cancelled = []

    async def _cancel(order_id, symbol, client=None):
        cancelled.append(order_id)
        return True

    eng.stop_loss_manager.cancel_stop_loss = _cancel
    res = await eng.execute_exit_fast({**pos, "stop_loss_order_id": "sl-1"})
    await eng.finalize_closed_positions([res])
    assert cancelled == []                                         # the open position keeps its stop


async def test_a_sent_fast_close_stays_a_success_when_bookkeeping_fails(db, monkeypatch):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()

    async def _broken(*a, **k):
        raise RuntimeError("stop cancel blew up")

    monkeypatch.setattr(eng, "_cancel_separate_stop", _broken)
    res = await eng.execute_exit_fast(pos)
    assert res["success"] and eng._close_owned(pos["id"])          # not retried: the order was sent


async def test_an_emergency_wait_that_times_out_is_reported(db, monkeypatch):
    import asyncio
    eng, _ = await _engine(db, FakeBybit())
    stuck = asyncio.get_running_loop().create_future()
    eng._entries_in_flight.add(stuck)
    original = eng._wait_for_entries_in_flight

    async def _short(timeout=30.0):
        return await original(timeout=0.01)

    monkeypatch.setattr(eng, "_wait_for_entries_in_flight", _short)
    res = await eng.close_all(reason="emergency_stop", stop_engine=True)
    assert not res["success"] and "still running" in res["errors"][0]["error"]


async def test_close_all_settles_an_unconfirmed_close_against_the_venue(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", r["position_id"])
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "2", "avgPrice": "100"}]   # 2 of 5 left
    await eng.close_all()
    (close,) = [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]
    assert close["qty"] == "2.0"


async def test_close_all_fails_when_recording_a_close_fails(db, monkeypatch):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    await eng.execute_entry(_sig())

    async def _broken(close, client):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(eng, "_finalize_one", _broken)
    res = await eng.close_all()
    assert not res["success"] and res["positions_closed"] == 0
    assert "recording the close failed" in res["errors"][0]["error"]


async def test_a_separate_stop_is_cancelled_once_the_close_is_confirmed(db):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    await eng.execute_entry(_sig())
    (pos,) = await eng.position_tracker.get_all_positions()
    cancelled = []

    async def _cancel(order_id, symbol, client=None):
        cancelled.append(order_id)
        return True

    eng.stop_loss_manager.cancel_stop_loss = _cancel
    res = await eng.execute_exit_fast({**pos, "stop_loss_order_id": "sl-1"})
    await eng.finalize_closed_positions([res])
    assert cancelled == ["sl-1"]


# ---- round 5 -----------------------------------------------------------------------------

async def test_reclosing_after_an_earlier_partial_fill_never_invents_its_price(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())                            # 5
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", r["position_id"])
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "2", "avgPrice": "100"}]   # 3 filled earlier
    fake.price = 110.0
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    (p,) = await _rows(db, "SELECT status, pnl, pnl_source FROM positions WHERE id = $1", r["position_id"])
    assert (p["status"], p["pnl"], p["pnl_source"]) == ("CLOSED", None, "pending")
    parts = await _rows(db, "SELECT qty, price FROM trades WHERE position_id = $1 AND trade_type = 'partial_exit'",
                        r["position_id"])
    assert [(x["qty"], x["price"]) for x in parts] == [(3.0, None)]  # the earlier fill: unknown price


async def test_reclosing_refuses_when_the_venue_holds_more_than_this_position(db):
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=False)
    r = await eng.execute_entry(_sig())
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", r["position_id"])
    fake.positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "9", "avgPrice": "100"}]   # someone else's too
    (pos,) = await eng.position_tracker.get_all_positions()
    with pytest.raises(Exception, match="check manually"):
        await eng.execute_exit(pos)
    assert not [kw for n, kw in fake.calls if n == "place_market_order" and kw.get("reduce_only")]


async def test_the_venue_check_before_a_reclose_passes_an_open_breaker(db):
    calls = []

    class _Bypass(FakeBybit):
        async def get_positions(self, category="linear", symbol=None, settle_coin=None, bypass_breaker=False):
            calls.append(bypass_breaker)
            return []

    eng, _ = await _engine(db, _Bypass(), dry_run=False)
    r = await eng.execute_entry(_sig())
    async with db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE id = $1", r["position_id"])
    (pos,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(pos)
    assert calls and calls[-1] is True


async def test_close_one_reports_success_when_the_other_close_finished_it(db, monkeypatch):
    import asyncio
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    r = await eng.execute_entry(_sig())
    eng._own_close(r["position_id"])

    async def _other_close_finishes():
        await _real_sleep(0.05)
        async with db.acquire() as c:
            await c.execute("UPDATE positions SET status = 'CLOSED' WHERE id = $1", r["position_id"])
        eng._release_close(r["position_id"])
    helper = asyncio.create_task(_other_close_finishes())
    res = await eng.close_all(reason="emergency_stop", stop_engine=True)
    await helper
    assert res["success"] and res["errors"] == []


async def test_an_entry_safety_close_handled_elsewhere_is_reported_as_such(db, monkeypatch):
    eng, _ = await _engine(db, FakeBybit(stop_ok=False), dry_run=False)

    async def _busy(position, reason="close_all", client=None):
        return {'success': True, 'in_progress': True, 'position_id': position['id'], 'symbol': 'BTCUSDT'}

    monkeypatch.setattr(eng, "execute_exit_fast", _busy)
    r = await eng.execute_entry(_sig())
    assert r["reason"] == "stop_failed_close_unconfirmed" and "another close" in r["close_error"]


async def test_close_all_looks_again_before_reporting_a_position_it_never_got_to_close(db, monkeypatch):
    eng, _ = await _engine(db, FakeBybit(), dry_run=False)
    r = await eng.execute_entry(_sig())

    async def _always_busy(position, reason="manual", client=None):
        async with db.acquire() as c:                              # meanwhile the other close finished it
            await c.execute("UPDATE positions SET status = 'CLOSED' WHERE id = $1", r["position_id"])
        return "in_progress"

    async def _released(pid, timeout=30.0):
        return True

    monkeypatch.setattr(eng, "execute_exit", _always_busy)
    monkeypatch.setattr(eng, "_wait_close_released", _released)
    res = await eng.close_all(reason="emergency_stop", stop_engine=True)
    assert res["success"] and res["positions_closed"] == 1
