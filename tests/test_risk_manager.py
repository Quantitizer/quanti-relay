from datetime import UTC, datetime, timedelta

from relay.execution.risk_manager import RiskManager


class _Settings:
    def __init__(self, **kw):
        self.values = kw

    async def get_execution_settings(self):
        return self.values


class _Bybit:
    def __init__(self, available=1000.0, source="wallet"):
        self.available, self.source = available, source

    async def get_balance_safe(self, coin="USDT"):
        return self.available, self.source


async def _open(db, symbol, side="LONG", entry=100.0, size=1.0):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        await c.execute(
            "INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, created_at,"
            " updated_at) VALUES ($1, $2, $3, $4, 'OPEN', $5, $5, $5)", symbol, side, size, entry, now)


async def _closed(db, pnl, source, closed_at=None):
    t = closed_at or datetime.now(UTC)
    async with db.acquire() as c:
        await c.execute(
            "INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, closed_at, pnl,"
            " pnl_source, created_at, updated_at) VALUES ('X', 'LONG', 1, 1, 'CLOSED', $1, $1, $2, $3, $1, $1)",
            t, pnl, source)


def _rm(db, bybit=None, **settings):
    return RiskManager(db, bybit or _Bybit(), _Settings(**settings))


async def test_approves_and_reports_available_balance(db):
    r = await _rm(db).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert r == {"approved": True, "reason": "All risk checks passed", "max_size": 1000.0}


async def test_below_minimum_balance_is_rejected(db):
    rm = RiskManager(db, _Bybit(available=50.0), _Settings(), min_balance=100.0)
    r = await rm.check_trade_approval("BTCUSDT", {"direction": "long"})
    assert not r["approved"] and "Insufficient balance" in r["reason"]


async def test_no_limit_is_preset(db):
    """The relay ships no trading numbers: with nothing configured, a tiny balance and a crowded
    book pass every limit (only a missing balance or a same-symbol conflict can still refuse)."""
    for i in range(50):
        await _open(db, f"S{i}USDT", entry=100.0, size=100.0)   # far beyond any plausible preset
    await _closed(db, -900.0, "venue_api")                       # 90% daily loss
    r = await _rm(db, _Bybit(available=1.0)).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert r["approved"], r


async def test_unknown_balance_fails_closed(db):
    r = await _rm(db, _Bybit(available=0.0, source="error")).check_trade_approval("BTCUSDT", {})
    assert not r["approved"] and "source: error" in r["reason"]


async def test_position_count_limit(db):
    await _open(db, "AUSDT")
    await _open(db, "BUSDT")
    r = await _rm(db, max_positions=2).check_trade_approval("CUSDT", {"direction": "long"})
    assert not r["approved"] and "Position limit reached: 2/2" in r["reason"]


async def test_exposure_limit(db):
    await _open(db, "AUSDT", entry=300.0, size=1.0)  # 300 of 1000 = 30%
    r = await _rm(db, max_exposure_pct=30.0).check_trade_approval("BUSDT", {"direction": "long"})
    assert not r["approved"] and "Exposure limit" in r["reason"]


async def test_one_position_per_symbol_whatever_the_settings(db):
    """Pyramiding and reversal are not supported: a second order on a held symbol is refused, and
    the old switches that pretended otherwise are gone."""
    await _open(db, "BTCUSDT", side="LONG")
    for settings in ({}, {"allow_pyramiding": True, "ignore_signals_when_in_position": False},
                     {"counter_signal_reverses": True, "ignore_signals_when_in_position": False}):
        for direction in ("long", "short"):
            r = await _rm(db, **settings).check_trade_approval("BTCUSDT", {"direction": direction})
            assert not r["approved"] and "already has an open position" in r["reason"]


async def test_a_daily_loss_check_that_cannot_run_refuses(db, monkeypatch):
    rm = _rm(db, daily_loss_limit_pct=5.0)

    def _broken(*a, **k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(rm.db, "acquire", _broken)
    r = await rm._check_daily_loss_limit(1000.0)
    assert not r["approved"] and "Daily loss check failed" in r["reason"]


async def test_the_new_order_counts_toward_the_exposure_limit(db):
    await _open(db, "AUSDT", entry=100.0, size=2.0)              # 200 open on a 1,000 account = 20%
    rm = _rm(db, max_exposure_pct=30.0)
    assert (await rm.check_trade_approval("BUSDT", {"direction": "long"}))["approved"]   # 20% < 30% before
    assert (await rm.check_new_exposure(50.0))["approved"]                               # 25% after
    r = await rm.check_new_exposure(150.0)                                                # 35% after
    assert not r["approved"] and "35.0%" in r["reason"]


async def test_zero_equity_is_reported_as_zero_not_as_unreadable(db):
    r = await _rm(db, _Bybit(available=0.0)).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert not r["approved"] and r["reason"] == "Account equity is 0"


async def test_daily_loss_counts_todays_trusted_pnl(db):
    await _closed(db, -30.0, "venue_api")
    await _closed(db, -500.0, "venue_api", datetime.now(UTC) - timedelta(days=2))  # not today
    assert (await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("X", {}))["approved"]  # 3%
    r = await _rm(db, daily_loss_limit_pct=3.0).check_trade_approval("X", {})
    assert not r["approved"] and "Daily loss limit breached" in r["reason"]


async def test_an_old_style_estimate_today_makes_the_day_unknown(db):
    """An estimate (e.g. a ticker guess) is not the real result: it is neither trusted nor ignored."""
    await _closed(db, -500.0, "ticker_fallback")
    r = await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("X", {})
    assert not r["approved"] and "Daily loss unknown" in r["reason"]


# ---- paper and live positions never limit each other ----------------------------------

async def _paper(db, symbol, side="LONG"):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        await c.execute(
            "INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, created_at,"
            " updated_at, dry_run) VALUES ($1, $2, 1, 100, 'OPEN', $3, $3, $3, $4)", symbol, side, now, True)


async def test_live_positions_do_not_limit_paper_trades(db):
    """Real positions on the account (e.g. imported) must not block simulated orders — and a
    simulated order must not slip past them in live mode."""
    for s in ("A1USDT", "A2USDT", "BTCUSDT"):
        await _open(db, s)  # live
    paper = RiskManager(db, _Bybit(), _Settings(max_positions=2), dry_run=True)
    live = RiskManager(db, _Bybit(), _Settings(max_positions=2), dry_run=False)
    assert (await paper.check_trade_approval("BTCUSDT", {"direction": "long"}))["approved"]
    r = await live.check_trade_approval("ETHUSDT", {"direction": "long"})
    assert not r["approved"] and r["reason"] == "Position limit reached: 3/2"


async def test_paper_positions_do_not_limit_live_trades(db):
    for s in ("A1USDT", "A2USDT", "BTCUSDT"):
        await _paper(db, s)
    live = RiskManager(db, _Bybit(), _Settings(max_positions=2), dry_run=False)
    paper = RiskManager(db, _Bybit(), _Settings(max_positions=2), dry_run=True)
    assert (await live.check_trade_approval("BTCUSDT", {"direction": "long"}))["approved"]
    assert not (await paper.check_trade_approval("ETHUSDT", {"direction": "long"}))["approved"]


async def test_symbol_conflict_is_per_mode(db):
    await _open(db, "BTCUSDT")  # live
    paper = RiskManager(db, _Bybit(), _Settings(), dry_run=True)
    assert (await paper.check_trade_approval("BTCUSDT", {"direction": "long"}))["approved"]
    await _paper(db, "BTCUSDT")
    assert not (await paper.check_trade_approval("BTCUSDT", {"direction": "long"}))["approved"]


async def test_exposure_is_per_mode(db):
    await _open(db, "BTCUSDT", entry=100.0, size=5.0)  # live notional 500 of a 1000 balance = 50%
    paper = RiskManager(db, _Bybit(), _Settings(max_exposure_pct=30.0), dry_run=True)
    live = RiskManager(db, _Bybit(), _Settings(max_exposure_pct=30.0), dry_run=False)
    assert (await paper.check_trade_approval("ETHUSDT", {"direction": "long"}))["approved"]
    assert "Exposure limit" in (await live.check_trade_approval("ETHUSDT", {"direction": "long"}))["reason"]


async def test_daily_loss_counts_only_this_modes_results(db):
    await _closed(db, -200.0, "venue_api")  # live loss: 20% of balance
    paper = RiskManager(db, _Bybit(), _Settings(daily_loss_limit_pct=5.0), dry_run=True)
    live = RiskManager(db, _Bybit(), _Settings(daily_loss_limit_pct=5.0), dry_run=False)
    assert (await paper.check_trade_approval("ETHUSDT", {"direction": "long"}))["approved"]
    assert not (await live.check_trade_approval("ETHUSDT", {"direction": "long"}))["approved"]


async def test_daily_loss_counts_the_relays_own_fill_based_results(db):
    await _closed(db, -80.0, "local_calc")                       # 8% of 1,000
    r = await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert not r["approved"] and "Daily loss limit breached" in r["reason"]


async def test_an_unpriced_close_today_makes_the_daily_loss_unknown_and_refuses(db):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        await c.execute(
            "INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, closed_at, pnl,"
            " pnl_source, created_at, updated_at) VALUES ('X', 'LONG', 1, 1, 'CLOSED', $1, $1, NULL, 'pending', $1, $1)",
            now)
    r = await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert not r["approved"] and "Daily loss unknown" in r["reason"]
    assert (await _rm(db).check_trade_approval("BTCUSDT", {"direction": "long"}))["approved"]   # no limit set


async def _with_partial(db, *, closed: bool, position_pnl, partial_pnl, partial_when, pnl_source="local_calc"):
    import json
    now = datetime.now(UTC)
    async with db.acquire() as c:
        pid = await c.fetchval(
            "INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, closed_at, pnl, pnl_source,"
            " created_at, updated_at) VALUES ('P', 'LONG', 1, 100, $1, $2, $3, $4, $5, $2, $2) RETURNING id",
            "CLOSED" if closed else "OPEN", now - timedelta(days=2), now if closed else None,
            position_pnl if closed else None, pnl_source if closed else None)
        await c.execute(
            "INSERT INTO trades (position_id, symbol, trade_type, side, qty, price, time, metadata, created_at,"
            " updated_at) VALUES ($1, 'P', 'partial_exit', 'SELL', 1, 90, $2, $3, $2, $2)",
            pid, partial_when, json.dumps({"pnl": partial_pnl}))


async def test_a_partial_loss_today_counts_today_even_while_the_position_is_open(db):
    await _with_partial(db, closed=False, position_pnl=None, partial_pnl=-80.0, partial_when=datetime.now(UTC))
    r = await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert not r["approved"] and "breached: 8.0%" in r["reason"]


async def test_a_partial_from_yesterday_is_not_todays_loss(db):
    """Closed today for -100 in total, of which -80 was a partial yesterday: today's loss is -20 (2%)."""
    await _with_partial(db, closed=True, position_pnl=-100.0, partial_pnl=-80.0,
                        partial_when=datetime.now(UTC) - timedelta(days=1))
    assert (await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("BTCUSDT", {"direction": "long"}))["approved"]


async def test_the_unknown_daily_loss_refusal_names_the_closes(db):
    now = datetime.now(UTC)
    async with db.acquire() as c:
        await c.execute(
            "INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, closed_at, pnl,"
            " pnl_source, created_at, updated_at) VALUES ('ETHUSDT', 'LONG', 1, 1, 'CLOSED', $1, $1, NULL, 'pending', $1, $1)",
            now)
    r = await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert "ETHUSDT #" in r["reason"]


async def test_an_unknown_partial_does_not_block_once_its_position_closed_today_with_a_known_result(db):
    # Only a venue result can cover a partial whose own PnL is unknown (it is a window sum).
    await _with_partial(db, closed=True, position_pnl=-20.0, partial_pnl=None, partial_when=datetime.now(UTC),
                        pnl_source="venue_api")
    assert (await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("BTCUSDT", {"direction": "long"}))["approved"]


async def test_an_unknown_partial_today_on_an_open_position_is_unknown(db):
    await _with_partial(db, closed=False, position_pnl=None, partial_pnl=None, partial_when=datetime.now(UTC))
    r = await _rm(db, daily_loss_limit_pct=5.0).check_trade_approval("BTCUSDT", {"direction": "long"})
    assert not r["approved"] and "partial close of position" in r["reason"]
