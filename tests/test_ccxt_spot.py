"""Spot through CCXT: long only, the coins held are the position, and the stop holds the coins
(tests/fake_ccxt.py FakeSpot)."""
import ccxt
import pytest

from relay.deployment_config import Venue
from relay.venue.ccxt_spot import CcxtSpotVenue
from relay.venue.ccxt_venue import VenueRejected
from relay.venue.factory import build_ccxt_venue, venue_problems
from relay.venue.profiles import Profile
from tests.fake_ccxt import FakeSpot

PROFILE = Profile("fakex", "a fake exchange")
BTC = "BTC/USDT"


def _venue(**kw):
    x = FakeSpot(**kw)
    return CcxtSpotVenue(x, PROFILE), x


def _open_stops(x):
    return [o for o in x.orders.values() if o["status"] == "open" and o.get("triggerPrice")]


async def _bought(v, x, qty="2"):
    await v.place_market_order("BTCUSDT", "Buy", qty)
    return x


# ------------------------------------------------------------------ markets and holdings
async def test_only_usdt_spot_markets_and_the_venue_is_told_apart_from_the_perpetuals():
    v, x = _venue()
    assert v.venue_id == "fakexspot" and v.spot and not v.can_short and v.can_stop
    assert sorted(r["symbol"] for r in await v.list_instruments()) == ["BTCUSDT", "ETHUSDT"]
    (btc,) = await v.list_instruments(symbol="BTCUSDT")
    assert btc["lotSizeFilter"]["qtyStep"] == "0.0001"                 # coins, no contracts
    with pytest.raises(VenueRejected, match="no USDT spot market for SOLUSDT"):
        await v.get_ticker("SOLUSDT")


async def test_the_coins_held_are_the_position_at_the_cost_of_the_buys_that_make_it_up():
    v, x = _venue(price=100.0)
    await _bought(v, x, "1")
    x.prices[BTC] = 110.0
    await _bought(v, x, "1")
    x.prices[BTC] = 120.0
    (p,) = await v.get_positions()
    assert (p["symbol"], p["side"], p["size"], p["leverage"]) == ("BTCUSDT", "Buy", "2", "1")
    assert p["avgPrice"] == "105" and p["markPrice"] == "120" and p["unrealisedPnl"] == "30"
    assert p["stopLoss"] == "" and p["createdTime"] == str(x.trades[0]["timestamp"])
    assert await v.get_positions(symbol="ETHUSDT") == []


async def test_dust_and_coins_without_fills_read_honestly():
    v, x = _venue(price=100.0)
    x.total["ETH"] = 0.0005                                           # below the minimum amount: dust
    x.total["BTC"] = 0.04                                             # 4 USDT: below the minimum order value
    assert await v.get_positions() == []
    x.total["BTC"] = 1.0                                              # held, but no fill explains it
    (p,) = await v.get_positions()
    assert p["size"] == "1" and p["avgPrice"] == "" and p["unrealisedPnl"] == "" and p["createdTime"] == ""
    x.fail["fetch_my_trades"] = ccxt.ExchangeNotAvailable("fake: down")
    (p,) = await v.get_positions()
    assert p["avgPrice"] == ""                                        # unreadable fills: unknown, not 0


async def test_equity_is_the_cash_plus_the_coins():
    v, x = _venue(price=100.0, usdt=1000.0)
    await _bought(v, x, "2")
    x.prices[BTC] = 150.0
    assert await v.get_balance_safe() == (800.0 + 300.0, "wallet")


async def test_prepare_names_the_coins_the_account_already_holds():
    v, x = _venue()
    assert await v.prepare() == []
    x.total["BTC"] = 0.5
    (w,) = await v.prepare()
    assert "0.5 BTC" in w and "dedicated_account" in w


# ------------------------------------------------------------------ long only
async def test_a_short_is_refused_and_a_buy_sends_a_price_only_where_the_venue_needs_one():
    v, x = _venue(price=100.0)
    with pytest.raises(VenueRejected, match="no short selling on spot"):
        await v.place_market_order("BTCUSDT", "Sell", "1")
    assert "create_order" not in x.names()
    await v.place_market_order("BTCUSDT", "Buy", "1", order_link_id="rl-d1-abc")
    (_, args, kw) = [c for c in x.calls if c[0] == "create_order"][-1]
    assert args == (BTC, "market", "buy", 1.0, None) and kw["params"]["clientOrderId"] == "rl-d1-abc"
    for options in ({"createMarketBuyOrderRequiresPrice": True},
                    {"createOrder": {"createMarketBuyOrderRequiresPrice": True}}):
        v, x = _venue(price=100.0, options=options)
        await v.place_market_order("BTCUSDT", "Buy", "1")
        assert [c for c in x.calls if c[0] == "create_order"][-1][1][4] == 100.0
    with pytest.raises(VenueRejected, match="beside the stop"):
        await v.place_limit_order("BTCUSDT", "Sell", "1", "120", reduce_only=True)


# ------------------------------------------------------------------ the stop holds the coins
async def test_the_stop_is_a_resting_sell_that_holds_the_coins_and_shows_on_the_position():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 0
    (stop,) = _open_stops(x)
    assert (stop["side"], stop["amount"], stop["stopLossPrice"]) == ("sell", 2.0, 95.0)
    assert x.free("BTC") == 0                                          # held by the stop
    (p,) = await v.get_positions()
    assert p["stopLoss"] == "95"
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 34040
    x.prices[BTC] = 94.0
    x.trigger(BTC)
    assert await v.get_positions() == [] and x.total["BTC"] == 0


async def test_moving_the_stop_cancels_the_old_one_first_and_restores_it_if_the_new_one_is_refused():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.calls.clear()
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=97.0))["retCode"] == 0
    assert [c[0] for c in x.calls if c[0] in ("create_order", "cancel_order")] == ["cancel_order", "create_order"]
    assert [o["stopLossPrice"] for o in _open_stops(x)] == [97.0]
    x.fail["create_order"] = ccxt.InvalidOrder("fake: trigger price invalid")
    r = await v.set_trading_stop("BTCUSDT", stop_loss=101.0)
    assert r["retCode"] == -1 and "invalid order" in r["retMsg"]
    assert [o["stopLossPrice"] for o in _open_stops(x)] == [97.0]     # put back
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=0))["retCode"] == 0
    assert _open_stops(x) == []


async def test_a_close_frees_the_coins_from_the_stop_and_leaves_nothing_resting():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    await v.place_market_order("BTCUSDT", "Sell", "2", reduce_only=True)
    assert x.total["BTC"] == 0 and _open_stops(x) == []


async def test_a_partial_close_puts_the_stop_back_on_what_is_left():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    await v.place_market_order("BTCUSDT", "Sell", "0.5", reduce_only=True)
    assert x.total["BTC"] == 1.5
    (stop,) = _open_stops(x)
    assert (stop["amount"], stop["stopLossPrice"]) == (1.5, 95.0)


async def test_a_close_never_sells_more_than_is_held_and_a_refused_one_keeps_the_stop():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.fail["create_order"] = ccxt.ExchangeError("fake: market closed")
    with pytest.raises(VenueRejected):
        await v.place_market_order("BTCUSDT", "Sell", "2", reduce_only=True)
    (stop,) = _open_stops(x)
    assert (stop["amount"], stop["stopLossPrice"]) == (2.0, 95.0)      # straight back
    x.total["BTC"] = 1.9                                               # a fee took some coin
    x.locked["BTC"] = 0.0
    stop["status"] = "canceled"
    await v.place_market_order("BTCUSDT", "Sell", "2", reduce_only=True)
    assert x.total["BTC"] == 0


async def test_a_close_gets_through_an_open_breaker():
    from relay.util.circuit_breaker import CircuitBreakerOpen
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    for _ in range(v.circuit_breaker.failure_threshold):
        x.fail["fetch_ticker"] = ccxt.ExchangeNotAvailable("fake: down")
        with pytest.raises(ConnectionError):
            await v.get_ticker("BTCUSDT")
    with pytest.raises(CircuitBreakerOpen):
        await v.place_market_order("BTCUSDT", "Buy", "1")
    await v.place_market_order("BTCUSDT", "Sell", "2", reduce_only=True)
    assert x.total["BTC"] == 0


async def test_what_spot_cannot_do_answers_like_bybit():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    assert (await v.set_trading_stop("BTCUSDT", take_profit=120.0))["retCode"] == -1
    assert (await v.set_trading_stop("BTCUSDT", trailing_stop=2.0))["retCode"] == -1
    assert (await v.set_leverage("BTCUSDT", 1))["retCode"] == 110043
    assert (await v.set_leverage("BTCUSDT", 2))["retCode"] == -1
    assert (await v.switch_isolated_margin("BTCUSDT", 1))["retCode"] == -1
    assert await v.get_transaction_log(start_time=0) == []            # no funding on spot
    assert _open_stops(x) == []


def test_a_spot_market_without_stop_orders_is_known():
    v, _ = _venue(features={"spot": {"createOrder": {"stopLossPrice": False, "triggerPrice": True}}})
    assert not v.can_stop
    assert not _venue(features={})[0].can_stop                        # unknown counts as no


# ------------------------------------------------------------------ the engine on top
async def test_the_engine_buys_protects_and_sells_on_spot(db):
    from tests.test_engine import SETTINGS, _engine, _no_sleep, _rows, _sig  # noqa: F401
    v, x = _venue(price=100.0)
    eng, _ = await _engine(db, v, dry_run=False, settings={**SETTINGS, "leverage": None})
    r = await eng.execute_entry(_sig())                                # 500 USDT at 100 = 5 BTC
    assert r["success"], r
    assert x.total["BTC"] == 5
    (pos,) = await _rows(db, "SELECT size, entry_price, stop_price, status FROM positions")
    assert (pos["size"], pos["entry_price"], pos["stop_price"], pos["status"]) == (5.0, 100.0, 95.0, "OPEN")
    (stop,) = _open_stops(x)
    assert stop["amount"] == 5
    r = await eng.execute_entry(_sig(symbol="ETHUSDT", direction="short", stop_price=105.0))
    assert r == {"success": False, "reason": "short_unsupported"}
    (row,) = await eng.position_tracker.get_all_positions()
    x.prices[BTC] = 110.0
    await eng.execute_exit(row)
    assert x.total["BTC"] == 0 and _open_stops(x) == []
    (pos,) = await _rows(db, "SELECT status, exit_price FROM positions")
    assert (pos["status"], pos["exit_price"]) == ("CLOSED", 110.0)


async def test_the_engine_sees_a_spot_stop_that_fired(db):
    from tests.test_engine import SETTINGS, _engine, _no_sleep, _rows, _sig  # noqa: F401
    v, x = _venue(price=100.0)
    eng, _ = await _engine(db, v, dry_run=False, settings={**SETTINGS, "leverage": None})
    async with db.acquire() as c:
        await c.execute("UPDATE accounts SET venue = 'fakexspot'")
    assert (await eng.execute_entry(_sig()))["success"]
    x.prices[BTC] = 94.0
    x.trigger(BTC)
    await eng.position_tracker.sync_all_positions()
    (pos,) = await _rows(db, "SELECT status FROM positions")
    assert pos["status"] == "CLOSED"


# ------------------------------------------------------------------ configuration
def test_spot_problems_refuse_what_cannot_rest_beside_the_stop():
    refuse, warn = venue_problems(PROFILE, {}, {"leverage": 2, "isolated_margin": True, "take_profit_pct": 3,
                                                "take_profit_ladder": [{"pct": 2}], "trailing_distance_pct": 1,
                                                "daily_loss_limit_pct": 2},
                                  spot_features={"createOrder": {"stopLossPrice": True}})
    assert sorted(r.split(" is set")[0] for r in refuse) == ["isolated_margin", "leverage", "take_profit_ladder",
                                                              "take_profit_pct", "trailing_distance_pct"]
    assert any("long only" in w for w in warn) and any("daily_loss_limit_pct" in w for w in warn)
    assert venue_problems(PROFILE, {}, {"leverage": None, "isolated_margin": None},
                          spot_features={"createOrder": {"stopLossPrice": True}})[0] == []
    refuse, _ = venue_problems(PROFILE, {"createStopLossOrder": True}, {}, spot_features={})
    assert len(refuse) == 1 and "fakex spot cannot hold a stop" in refuse[0]          # the perps' flags do not count
    refuse, warn = venue_problems(PROFILE, {}, {"require_stop": False}, spot_features={})
    assert refuse == [] and any("no stop on the exchange" in w for w in warn)


@pytest.mark.parametrize("fields, error", [
    ({"exchange": "okx", "environment": "testnet", "market": "spot"}, "dedicated_account: true"),
    ({"exchange": "okx", "environment": "testnet", "dedicated_account": True}, "spot only"),
    ({"exchange": "bybit", "market": "spot", "dedicated_account": True}, "environment is required for bybitspot"),
    ({"exchange": "a" * 27, "environment": "live", "market": "spot", "dedicated_account": True}, "too long"),
])
def test_the_spot_venue_section_is_checked(fields, error):
    with pytest.raises(ValueError, match=error):
        Venue(**fields)


def test_spot_is_recorded_under_its_own_venue_id():
    v = Venue(exchange="bybit", environment="testnet", market="spot", dedicated_account=True)
    assert (v.native, v.venue_id) == (False, "bybitspot")
    assert (Venue().native, Venue().venue_id) == (True, "bybit")
    assert Venue(exchange="okx", environment="live").venue_id == "okx"


def test_the_factory_builds_a_spot_client_for_the_spot_market():
    v = build_ccxt_venue("okx", "testnet", paper=True, environ={}, market="spot")
    assert isinstance(v, CcxtSpotVenue) and v.x.options["defaultType"] == "spot" and v.venue_id == "okxspot"
    p = build_ccxt_venue("okx", "testnet", paper=True, environ={})
    assert not isinstance(p, CcxtSpotVenue) and p.x.options["defaultType"] == "swap"


# ------------------------------------------------------------------ the runtime
SPOT_FILE = "venue:\n  exchange: fakex\n  environment: testnet\n  market: spot\n  dedicated_account: true\n"


def _rt(db, tmp_path, text=SPOT_FILE, x=None):
    from relay.runtime import Runtime
    from tests.test_venue_setup import _cfg, _deployment
    x = x or FakeSpot()
    return Runtime(_cfg(config_dir=str(_deployment(tmp_path, text))), db=db, client=CcxtSpotVenue(x, PROFILE)), x


async def test_the_relay_runs_on_spot_under_its_own_venue_id(db, tmp_path):
    rt, x = _rt(db, tmp_path)
    await rt.start()
    try:
        assert rt.venue == ("fakexspot", "testnet")
        async with db.acquire() as c:
            (acc,) = await c.fetch("SELECT venue FROM accounts")
        assert acc["venue"] == "fakexspot"
    finally:
        await rt.stop()


async def test_spot_settings_that_cannot_rest_beside_the_stop_stop_the_relay(db, tmp_path):
    from relay.venue.factory import VenueConfigError
    rt, _ = _rt(db, tmp_path, SPOT_FILE + "leverage: 2\n")
    with pytest.raises(VenueConfigError, match="spot has no leverage"):
        await rt.start()
    await rt.stop()


async def test_moving_from_the_perpetuals_to_spot_with_live_positions_open_is_refused(db, tmp_path):
    from relay.venue.factory import VenueConfigError
    from tests.test_venue_setup import _open_live_position
    await db.create_all()
    await _open_live_position(db, venue="fakex")
    rt, _ = _rt(db, tmp_path)
    with pytest.raises(VenueConfigError, match=r"open on fakex \(testnet\).*fakexspot \(testnet\)"):
        await rt.start()
    await rt.stop()


def test_check_config_shows_spot(tmp_path, monkeypatch, capsys):
    from tests.test_venue_setup import _check
    code, out = _check(tmp_path, monkeypatch, capsys, "venue:\n  exchange: okx\n  environment: testnet\n"
                                                      "  market: spot\n  dedicated_account: true\n")
    assert code == 0 and "venue: okx spot (long only, no leverage), environment: testnet" in out
    assert "long only - a short signal, a flip to short" in out
    code, out = _check(tmp_path, monkeypatch, capsys, "venue:\n  exchange: mexc\n  environment: live\n"
                                                      "  market: spot\n  dedicated_account: true\n")
    assert code != 0 and "mexc spot cannot hold a stop order" in out     # CCXT: no spot stops on MEXC


# ------------------------------------------------------------------ the edges
async def test_a_coin_quoted_in_another_currency_is_no_market():
    v, x = _venue()
    x.total["SOL"] = 10.0
    assert "SOLUSDT" not in [r["symbol"] for r in await v.list_instruments()]
    assert await v.get_positions() == []


async def test_the_stop_covers_the_holding_rounded_down_to_the_lot_step():
    v, x = _venue(price=100.0)
    x.total["BTC"] = 1.23456
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 0
    (stop,) = _open_stops(x)
    assert stop["amount"] == pytest.approx(1.2345)
    (p,) = await v.get_positions()
    assert p["stopLoss"] == "95"                                       # covers all that can be sold


async def test_less_than_the_minimum_amount_is_dust_even_when_it_is_worth_more_than_the_minimum_order():
    v, x = _venue()
    x.prices["ETH/USDT"] = 100_000.0
    x.total["ETH"] = 0.0005                                           # worth 50 USDT, below 0.001 ETH
    assert await v.get_positions() == []


async def test_the_average_cost_counts_only_the_buys_that_make_up_the_holding():
    v, x = _venue(price=100.0)
    await _bought(v, x, "1")
    x.prices[BTC] = 120.0
    await v.place_market_order("BTCUSDT", "Sell", "0.5", reduce_only=True)
    x.prices[BTC] = 110.0
    await _bought(v, x, "1")                                           # holds 1.5: 1 at 110 + 0.5 at 100
    (p,) = await v.get_positions()
    assert float(p["avgPrice"]) == pytest.approx((110 + 50) / 1.5)


async def test_coins_the_fills_do_not_explain_leave_the_cost_unknown():
    v, x = _venue(price=100.0)
    await _bought(v, x, "1")
    x.total["BTC"] = 3.0                                               # 2 more arrived without a fill
    (p,) = await v.get_positions()
    assert p["avgPrice"] == "" and p["createdTime"] == ""


async def test_a_holding_that_grew_past_its_stop_shows_no_stop():
    v, x = _venue(price=100.0)
    await _bought(v, x, "1")
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    await _bought(v, x, "1")
    (p,) = await v.get_positions()
    assert p["size"] == "2" and p["stopLoss"] == ""                    # the stop audit re-protects it


async def test_spot_refuses_a_trailing_stop_even_where_the_exchange_trails_perpetuals():
    from relay.venue.profiles import Capabilities
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, Profile("fakex", "trails", capabilities=Capabilities(trailing_stop=True)))
    x.has["createTrailingPercentOrder"] = True
    await _bought(v, x)
    r = await v.set_trading_stop("BTCUSDT", trailing_stop=2.0)
    assert r["retCode"] == -1 and "spot" in r["retMsg"]


async def test_spot_stops_are_ccxts_stop_market_sells_whatever_the_futures_style():
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, Profile("fakex", "direction", stop_style="direction",
                                 trigger_direction={"rising": {"triggerType": 1}, "falling": {"triggerType": 2}}))
    await _bought(v, x)
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 0
    (stop,) = _open_stops(x)
    assert stop["params"]["stopLossPrice"] == 95.0 and "triggerType" not in stop["params"]


async def test_the_runtime_builds_the_spot_client_for_a_spot_deployment(db, tmp_path, monkeypatch):
    import relay.runtime as runtime_mod
    from relay.runtime import Runtime
    from tests.test_venue_setup import _cfg, _deployment
    seen = {}

    def build(exchange, environment, *, paper, market="perpetual"):
        seen.update(exchange=exchange, environment=environment, market=market)
        return CcxtSpotVenue(FakeSpot(), PROFILE)
    monkeypatch.setattr(runtime_mod, "build_ccxt_venue", build)
    rt = Runtime(_cfg(config_dir=str(_deployment(tmp_path, SPOT_FILE))), db=db)
    await rt.start()
    await rt.stop()
    assert seen == {"exchange": "fakex", "environment": "testnet", "market": "spot"}


def test_empty_market_lines_read_as_the_perpetuals():
    from relay.deployment_config import DeploymentFile
    d = DeploymentFile.model_validate({"name": "x", "venue": {"exchange": "okx", "environment": "testnet",
                                                              "market": None, "dedicated_account": None}})
    assert (d.venue.market, d.venue.dedicated_account, d.venue.venue_id) == ("perpetual", False, "okx")


# ------------------------------------------------------------------ review round
async def test_a_closing_sell_is_an_order_for_coins_not_for_usdt():
    v, x = _venue(price=100.0)
    await _bought(v, x, "2")
    x.prices[BTC] = 90.0                                               # the price fell before the close
    await v.place_market_order("BTCUSDT", "Sell", "2", reduce_only=True)
    (_, args, _) = [c for c in x.calls if c[0] == "create_order"][-1]
    assert args[4] is None and x.total["BTC"] == 0                     # all 2 sold, none left behind


async def test_a_fee_taken_in_the_coin_is_not_recorded_as_held():
    v, x = _venue(price=100.0, coin_fee=0.001)
    r = await v.place_market_order("BTCUSDT", "Buy", "2")
    row = await v.get_order("BTCUSDT", order_id=r["orderId"])
    assert row["cumExecQty"] == "1.998" and x.total["BTC"] == 1.998
    x.fail["fetch_my_trades"] = ccxt.ExchangeNotAvailable("fake: down")
    assert (await v.get_order("BTCUSDT", order_id=r["orderId"]))["cumExecQty"] == "2"   # unreadable: gross


async def test_the_engine_closes_a_spot_position_whole_although_the_fee_was_in_the_coin(db):
    from tests.test_engine import SETTINGS, _engine, _no_sleep, _rows, _sig  # noqa: F401
    v, x = _venue(price=100.0, coin_fee=0.001)
    eng, _ = await _engine(db, v, dry_run=False, settings={**SETTINGS, "leverage": None})
    assert (await eng.execute_entry(_sig()))["success"]
    (pos,) = await _rows(db, "SELECT size FROM positions")
    assert pos["size"] == pytest.approx(4.995)                         # 5 bought, 0.005 kept as the fee
    (row,) = await eng.position_tracker.get_all_positions()
    await eng.execute_exit(row)
    (pos,) = await _rows(db, "SELECT status, size FROM positions")
    assert (pos["status"], pos["size"]) == ("CLOSED", pytest.approx(4.995)) and x.total["BTC"] == 0


async def test_spot_reports_no_closed_pnl_records():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.place_market_order("BTCUSDT", "Sell", "2", reduce_only=True)
    assert await v.get_closed_pnl(symbol="BTCUSDT", start_time=0) == []


async def test_a_sale_that_went_through_is_not_turned_into_an_error_by_the_stop_restore():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    real = v._restore_stop

    async def broken(*a):
        raise ConnectionError("fake: balance read lost")
    v._restore_stop = broken
    await v.place_market_order("BTCUSDT", "Sell", "0.5", reduce_only=True)     # no raise: no second sell
    assert x.total["BTC"] == 1.5
    v._restore_stop = real
    x.fail["create_order"] = ccxt.ExchangeError("fake: market closed")
    with pytest.raises(VenueRejected, match="market closed"):                  # the sell's own error stays
        await v.place_market_order("BTCUSDT", "Sell", "0.5", reduce_only=True)


async def test_the_restore_finds_a_stop_the_audit_already_put_back():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    real = v._holding

    async def audit_first(m):
        v._holding = real
        await v.set_trading_stop("BTCUSDT", stop_loss=95.0)            # the audit, between sale and restore
        return await real(m)
    v._holding = audit_first
    await v.place_market_order("BTCUSDT", "Sell", "0.5", reduce_only=True)
    assert [o["stopLossPrice"] for o in _open_stops(x)] == [95.0]


async def test_a_lost_answer_on_a_stop_that_was_placed_is_not_undone():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    real = x.create_order

    async def placed_but_lost(*a, **kw):
        await real(*a, **kw)
        raise ccxt.NetworkError("fake: answer lost")
    x.create_order = placed_but_lost
    r = await v.set_trading_stop("BTCUSDT", stop_loss=97.0)
    x.create_order = real
    assert r["retCode"] == 0 and [o["stopLossPrice"] for o in _open_stops(x)] == [97.0]


async def test_cancel_all_reaches_every_held_coin_and_its_stop_without_asking_for_positions():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.prices["ETH/USDT"] = 100.0
    await v.place_market_order("ETHUSDT", "Buy", "1")
    await v.place_limit_order("ETHUSDT", "Buy", "1", "90")
    await v.cancel_all_orders()
    assert not [o for o in x.orders.values() if o["status"] == "open"] and "fetch_positions" not in x.names()


# ------------------------------------------------------------------ review round: per-venue spot stops
async def test_bitget_spot_stops_are_asked_for_with_the_trigger_price():
    from relay.venue.profiles import PROFILES
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, PROFILES["bitget"])
    await _bought(v, x)
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 0
    (stop,) = _open_stops(x)
    assert stop["params"]["triggerPrice"] == 95.0 and "stopLossPrice" not in stop["params"]
    (p,) = await v.get_positions()
    assert p["stopLoss"] == "95"


async def test_a_gate_spot_stop_is_renewed_before_its_day_runs_out():
    from relay.venue.profiles import PROFILES
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, PROFILES["gate"])
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (stop,) = _open_stops(x)
    stop["timestamp"] -= 20 * 3600 * 1000                             # 20 of 24 hours gone
    (p,) = await v.get_positions()
    assert p["stopLoss"] == "95"                                       # renewed by the read (cancel first on spot)
    (fresh,) = _open_stops(x)
    assert fresh["id"] != stop["id"] and stop["status"] == "canceled"


def test_htx_spot_is_not_offered():
    from relay.venue.profiles import PROFILES
    refuse, _ = venue_problems(PROFILES["htx"], {}, {}, spot_features={"createOrder": {"stopLossPrice": True}})
    assert len(refuse) == 1 and "htx spot is not offered" in refuse[0] and "immediate market sell" in refuse[0]


async def test_spot_stops_are_refused_where_the_venue_drops_the_tag():
    v, x = _venue(price=100.0)
    x.drop_tags = True
    await _bought(v, x)
    r = await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    assert r["retCode"] == -1 and "tag back" in r["retMsg"] and _open_stops(x) == []


async def test_only_this_orders_fees_in_the_coin_are_subtracted():
    v, x = _venue(price=100.0, coin_fee=0.001)
    first = await v.place_market_order("BTCUSDT", "Buy", "2")
    second = await v.place_market_order("BTCUSDT", "Buy", "1")
    assert (await v.get_order("BTCUSDT", order_id=second["orderId"]))["cumExecQty"] == "0.999"
    assert (await v.get_order("BTCUSDT", order_id=first["orderId"]))["cumExecQty"] == "1.998"
    v2, x2 = _venue(price=100.0)                                       # fees in USDT: the coins are all held
    r = await v2.place_market_order("BTCUSDT", "Buy", "2")
    assert (await v2.get_order("BTCUSDT", order_id=r["orderId"]))["cumExecQty"] == "2"


async def test_the_restore_does_not_place_a_second_stop_beside_the_audits():
    v, x = _venue(price=100.0)
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    real = v._holding

    async def audit_first(m):
        v._holding = real
        await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
        return await real(m)
    v._holding = audit_first
    x.calls.clear()
    await v.place_market_order("BTCUSDT", "Sell", "0.5", reduce_only=True)
    assert x.names().count("create_order") == 2                       # the audit's stop and the sale, no more


async def test_spot_gives_no_pnl_records_even_where_the_futures_fills_carry_pnl():
    from relay.venue.profiles import PROFILES
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, PROFILES["binance"])
    await _bought(v, x)
    for t in x.trades:
        t["info"]["realizedPnl"] = "0"
    await v.place_market_order("BTCUSDT", "Sell", "2", reduce_only=True)
    assert await v.get_closed_pnl(symbol="BTCUSDT", start_time=0) == []


async def test_a_spot_profiles_extra_stop_parameters_are_sent():
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, Profile("fakex", "x", spot_trigger_params={"expiration": 3600}))
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (stop,) = _open_stops(x)
    assert stop["params"]["expiration"] == 3600


async def test_the_stop_audit_puts_back_a_spot_stop_whose_cost_is_unknown(db):
    from tests.test_engine import SETTINGS, _engine, _no_sleep, _sig  # noqa: F401
    v, x = _venue(price=100.0)
    eng, _ = await _engine(db, v, dry_run=False, settings={**SETTINGS, "leverage": None})
    async with db.acquire() as c:                                      # as the runtime records it
        await c.execute("UPDATE accounts SET venue = 'fakexspot'")
    assert (await eng.execute_entry(_sig()))["success"]
    for o in _open_stops(x):                                           # the stop is gone
        await x.cancel_order(o["id"], BTC)
    x.trades.clear()                                                   # and the fills no longer explain the coins
    (p,) = await v.get_positions()
    assert p["avgPrice"] == "" and p["stopLoss"] == ""
    out = await eng.audit_stops()
    assert [o["triggerPrice"] for o in _open_stops(x)] == [95.0], out


async def test_spot_cancel_all_reaches_a_resting_buy_on_a_coin_not_held():
    v, x = _venue(price=100.0)
    x.prices["ETH/USDT"] = 100.0
    await v.place_limit_order("ETHUSDT", "Buy", "1", "90")              # no ETH held
    await v.cancel_all_orders()
    assert not [o for o in x.orders.values() if o["status"] == "open"]


async def test_a_sale_puts_back_a_gate_stop_that_was_about_to_expire():
    from relay.venue.profiles import PROFILES
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, PROFILES["gate"])
    await _bought(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (stop,) = _open_stops(x)
    stop["timestamp"] -= 20 * 3600 * 1000
    await v.place_market_order("BTCUSDT", "Sell", "0.5", reduce_only=True)
    (back,) = _open_stops(x)
    assert (back["amount"], back["triggerPrice"]) == (1.5, 95.0)


async def test_spot_counts_as_holding_no_stop_once_the_venue_drops_the_tag():
    v, x = _venue(price=100.0)
    x.drop_tags = True
    await _bought(v, x)
    assert v.can_stop
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    assert not v.can_stop
