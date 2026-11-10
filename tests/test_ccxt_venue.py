"""The CCXT connector: Bybit-shaped answers from any exchange, without a network (tests/fake_ccxt.py)."""
import ccxt
import pytest

from relay.exchange.bybit.rest_client import is_benign_order_state
from relay.util.circuit_breaker import BenignOrderStateError
from relay.venue.ccxt_venue import CcxtVenue, VenueRejected, decode_client_id, encode_client_id
from relay.venue.profiles import Capabilities, Profile
from tests.fake_ccxt import FakeExchange

PROFILE = Profile("fakex", "a fake exchange", realized_pnl_field="pnl",
                  capabilities=Capabilities(trailing_stop=True, closed_pnl=True, funding_history=True))


def _venue(profile=PROFILE, **kw):
    x = FakeExchange(**kw)
    return CcxtVenue(x, profile), x


async def _long(v, x, qty="0.05"):
    await v.place_market_order("BTCUSDT", "Buy", qty)
    return x.positions["BTC/USDT:USDT"]


# ------------------------------------------------------------------ symbols and quantities
async def test_only_the_settled_linear_perpetuals_are_markets_and_quantities_are_in_coins():
    v, x = _venue()
    (btc,) = await v.list_instruments(symbol="BTCUSDT")
    assert btc["lotSizeFilter"] == {"qtyStep": "0.01", "minOrderQty": "0.01", "maxOrderQty": "1000",
                                    "minNotionalValue": ""}          # 1 contract = 0.01 BTC
    assert btc["priceFilter"]["tickSize"] == "0.1"
    (eth,) = await v.list_instruments(symbol="ethusdt")
    assert eth["lotSizeFilter"]["qtyStep"] == "0.01"
    assert sorted(r["symbol"] for r in await v.list_instruments()) == ["BTCUSDT", "ETHUSDT"]   # no spot/inverse/USDC
    assert await v.list_instruments(symbol="SOLUSDT") == []
    with pytest.raises(VenueRejected, match="no USDT-settled perpetual for SOLUSDT"):
        await v.get_ticker("SOLUSDT")


async def test_a_usdc_venue_trades_the_usdc_perpetual_for_a_usdt_symbol():
    v, x = _venue(Profile("fakex", "usdc venue", settle="USDC"))
    assert [r["symbol"] for r in await v.list_instruments()] == ["SOLUSDT"]
    await v.place_market_order("SOLUSDT", "Buy", "2.5")
    assert x.calls[-1][1][:4] == ("SOL/USDC:USDC", "market", "buy", 2.5)


@pytest.mark.parametrize("qty, contracts", [("0.03", 3), ("0.07", 7), ("0.29", 29), ("1.1", 110), ("0.01", 1)])
async def test_coins_become_whole_contracts_exactly(qty, contracts):
    v, x = _venue()
    await v.place_market_order("BTCUSDT", "Buy", qty)
    (_, args, _) = [c for c in x.calls if c[0] == "create_order"][-1]
    assert args[3] == contracts                                        # never 2.9999999 for 0.03
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["size"] == qty and p["side"] == "Buy"


async def test_a_position_reads_like_bybits():
    v, x = _venue(price=100.0)
    await _long(v, x)
    await v.place_market_order("ETHUSDT", "Sell", "1.5")
    rows = {p["symbol"]: p for p in await v.get_positions(settle_coin="USDT")}
    assert rows["BTCUSDT"]["side"] == "Buy" and rows["BTCUSDT"]["size"] == "0.05"
    assert rows["ETHUSDT"]["side"] == "Sell" and rows["ETHUSDT"]["avgPrice"] == "100"
    assert rows["BTCUSDT"]["stopLoss"] == ""                           # none yet
    t = await v.get_ticker("BTCUSDT")
    assert (t["lastPrice"], t["markPrice"]) == ("100", "100")


# ------------------------------------------------------------------ client order ids
@pytest.mark.parametrize("scheme", ["plain", "prefix_t", "alnum", "hex128"])
@pytest.mark.parametrize("link", ["rl-d7-0123456789abcd", "rl-d123456-ffffffffffffff", "rl-sl-0123456789ab",
                                  "rl-tp-abcdefabcdef", "rl-ts-000000000001"])
def test_the_relays_tags_survive_every_id_scheme(scheme, link):
    enc = encode_client_id(link, scheme)
    assert decode_client_id(enc, scheme) == link
    if scheme == "alnum":
        assert enc.isalnum() and len(enc) <= 32
    if scheme == "hex128":
        assert enc.startswith("0x") and len(enc) == 34 and int(enc, 0) >= 0
    if scheme == "prefix_t":
        assert enc.startswith("t-")


def test_a_foreign_id_reads_back_as_it_is():
    assert decode_client_id("my-own-order", "alnum") == "my-own-order"
    assert decode_client_id("0x" + "1" * 32, "hex128") == "0x" + "1" * 32
    assert decode_client_id(None, "plain") == ""


async def test_an_order_is_found_again_by_the_relays_tag():
    v, x = _venue(Profile("fakex", "okx-like", client_id="alnum"))
    await v.place_market_order("BTCUSDT", "Buy", "0.05", order_link_id="rl-d3-0123456789abcd")
    assert x.calls[-1][2]["params"]["clientOrderId"] == "rld3x0123456789abcd"
    o = await v.get_order("BTCUSDT", order_link_id="rl-d3-0123456789abcd")
    assert o["orderLinkId"] == "rl-d3-0123456789abcd" and o["orderStatus"] == "Filled"
    assert o["cumExecQty"] == "0.05" and o["avgPrice"] == "100"
    assert await v.get_order("BTCUSDT", order_link_id="rl-d3-ffffffffffffff") is None
    assert await v.get_order("BTCUSDT", order_id="nope") is None


@pytest.mark.parametrize("status, filled, bybit", [
    ("open", 0, "New"), ("open", 1, "PartiallyFilled"), ("closed", 5, "Filled"), ("canceled", 0, "Cancelled"),
    ("canceled", 2, "PartiallyFilledCanceled"), ("expired", 0, "Deactivated"), ("rejected", 0, "Rejected"),
])
async def test_order_states_read_like_bybits(status, filled, bybit):
    v, x = _venue()
    await v.place_limit_order("BTCUSDT", "Buy", "0.05", "90", post_only=False)
    (oid,) = list(x.orders)
    x.orders[oid].update(status=status, filled=filled)
    o = await v.get_order("BTCUSDT", order_id=oid)
    assert o["orderStatus"] == bybit
    assert o["cumExecQty"] == {0: "0", 1: "0.01", 2: "0.02", 5: "0.05"}[filled]      # contracts -> coins


# ------------------------------------------------------------------ the stop
async def test_the_stop_rests_on_the_exchange_and_shows_on_the_position():
    v, x = _venue()
    await _long(v, x)
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 0
    stop = [o for o in x.orders.values() if o.get("stopLossPrice")]
    assert len(stop) == 1 and stop[0]["side"] == "sell" and stop[0]["reduceOnly"] and stop[0]["amount"] == 5
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"
    x.prices["BTC/USDT:USDT"] = 94.0
    x.trigger("BTC/USDT:USDT")
    assert "BTC/USDT:USDT" not in x.positions                          # the stop closed it on the exchange


async def test_a_new_stop_is_placed_before_the_old_one_is_cancelled():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.calls.clear()
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=97.0))["retCode"] == 0
    writes = [c[0] for c in x.calls if c[0] in ("create_order", "cancel_order")]
    assert writes == ["create_order", "cancel_order"]
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "97"
    assert len([o for o in x.orders.values() if o.get("stopLossPrice") and o["status"] == "open"]) == 1


async def test_the_same_stop_again_is_not_modified_and_places_nothing():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.calls.clear()
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 34040
    assert "create_order" not in x.names()


async def test_a_refused_stop_keeps_the_old_one():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    x.fail["create_order"] = ccxt.InvalidOrder("fake: trigger price invalid")
    r = await v.set_trading_stop("BTCUSDT", stop_loss=101.0)
    assert r["retCode"] == -1 and "invalid order" in r["retMsg"]
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"


async def test_no_position_no_stop():
    v, _ = _venue()
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 10001


async def test_a_position_that_grew_past_its_stop_shows_no_stop_until_it_is_set_again():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    await v.place_market_order("BTCUSDT", "Buy", "0.02")              # added: 7 contracts, stop covers 5
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == ""                                         # the stop audit re-arms it
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 0
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"


async def test_stop_zero_removes_it_and_take_profit_is_its_own_order():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    assert (await v.set_trading_stop("BTCUSDT", take_profit=120.0))["retCode"] == 0
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"                                       # the take-profit is not the stop
    x.prices["BTC/USDT:USDT"] = 125.0                                  # past the take-profit, not fired yet
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"                                       # still not the stop
    x.prices["BTC/USDT:USDT"] = 100.0
    await v.set_trading_stop("BTCUSDT", take_profit=0)
    assert not [o for o in x.orders.values() if o.get("takeProfitPrice") and o["status"] == "open"]
    await v.set_trading_stop("BTCUSDT", stop_loss=0)
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == ""


async def test_the_users_own_trigger_orders_are_read_but_never_cancelled():
    v, x = _venue()
    await _long(v, x)
    await x.create_order("BTC/USDT:USDT", "market", "sell", 5, None, {"stopLossPrice": 90.0, "reduceOnly": True})
    await x.create_order("BTC/USDT:USDT", "market", "sell", 5, None, {"takeProfitPrice": 130.0, "reduceOnly": True})
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "90"                                       # theirs protects the position too
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"                                       # the tighter one
    await v.set_trading_stop("BTCUSDT", stop_loss=0)
    open_ = [o for o in x.orders.values() if o["status"] == "open"]
    assert sorted(str((o.get("stopLossPrice"), o.get("takeProfitPrice"))) for o in open_) == [
        "(90.0, None)", "(None, 130.0)"]


async def test_unreadable_stop_orders_fail_the_read_rather_than_hide_the_stop():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    real = x.fetch_open_orders

    async def broken(symbol=None, since=None, limit=None, params=None):
        if (params or {}).get("trigger"):
            raise ccxt.ExchangeError("fake: trigger orders unavailable")
        return await real(symbol, since, limit, params)
    x.fetch_open_orders = broken
    with pytest.raises(VenueRejected):
        await v.get_positions(symbol="BTCUSDT")


async def test_trailing_stops_where_the_venue_has_them():
    v, x = _venue()
    await _long(v, x)
    assert (await v.set_trading_stop("BTCUSDT", trailing_stop=2.0, active_price=110.0))["retCode"] == 0
    (_, args, _) = [c for c in x.calls if c[0] == "create_trailing_percent_order"][-1]
    assert args[1:6] == ("market", "sell", 5, pytest.approx(2.0 / 110 * 100), 110.0)
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == ""                                         # a trailing stop is not the fixed stop
    await v.set_trading_stop("BTCUSDT", trailing_stop=0)
    assert not [o for o in x.orders.values() if o.get("trailingPercent") and o["status"] == "open"]
    plain, px = _venue(Profile("fakex", "no trailing"))
    await _long(plain, px)
    r = await plain.set_trading_stop("BTCUSDT", trailing_stop=2.0)
    assert r["retCode"] == -1 and "not supported" in r["retMsg"]


# ------------------------------------------------------------------ the ladder
async def test_ladder_levels_rest_reduce_only_and_can_be_shrunk_or_cancelled_by_tag():
    v, x = _venue()
    await _long(v, x)
    r = await v.place_limit_order("BTCUSDT", "Sell", "0.02", "110", post_only=False, reduce_only=True,
                                  order_link_id="rl-d1-00000000000001")
    o = x.orders[r["orderId"]]
    assert (o["amount"], o["price"], o["reduceOnly"]) == (2, 110.0, True)
    assert r["orderId"] in [row["orderId"] for row in await v.get_open_orders(symbol="BTCUSDT")]
    await v.amend_order("BTCUSDT", order_id=r["orderId"], qty="0.01")
    assert x.orders[r["orderId"]]["amount"] == 1
    await v.cancel_order("BTCUSDT", order_link_id="rl-d1-00000000000001")
    assert x.orders[r["orderId"]]["status"] == "canceled"
    with pytest.raises(BenignOrderStateError) as e:
        await v.cancel_order("BTCUSDT", order_id=r["orderId"])        # already gone
    assert is_benign_order_state(str(e.value))


# ------------------------------------------------------------------ errors and answers
async def test_a_refusal_and_a_lost_answer_are_told_apart():
    v, x = _venue()
    x.fail["create_order"] = ccxt.InsufficientFunds("fake: margin is insufficient")
    with pytest.raises(VenueRejected) as e:
        await v.place_market_order("BTCUSDT", "Buy", "0.05")
    assert str(e.value).startswith("Venue rejected (fakex): insufficient balance")
    x.fail["create_order"] = ccxt.RequestTimeout("fake: timed out")
    with pytest.raises(ConnectionError, match="connection error"):
        await v.place_market_order("BTCUSDT", "Buy", "0.05")


async def test_leverage_and_margin_answer_like_bybit():
    v, x = _venue()
    assert (await v.set_leverage("BTCUSDT", 3))["retCode"] == 0
    assert (await v.set_leverage("BTCUSDT", 3))["retCode"] == 110043
    assert (await v.switch_isolated_margin("BTCUSDT", 3, isolated=True))["retCode"] == 0
    assert (await v.switch_isolated_margin("BTCUSDT", 3, isolated=True))["retCode"] == 110026
    x.fail["set_leverage"] = ccxt.BadRequest("fake: leverage too high")
    r = await v.set_leverage("BTCUSDT", 300)
    assert r["retCode"] == -1 and "leverage too high" in r["retMsg"]
    no_margin, _ = _venue(has={"setMarginMode": False})
    assert (await no_margin.switch_isolated_margin("BTCUSDT", 3))["retCode"] == -1


async def test_equity_is_marked_to_market():
    v, x = _venue(balance=1000.0)
    await _long(v, x)
    x.upl = -25.0
    assert await v.get_balance_safe() == (975.0, "wallet")
    included, xi = _venue(Profile("fakex", "total has upl", balance_includes_upl=True), balance=1000.0)
    await _long(included, xi)
    xi.upl = -25.0
    assert await included.get_balance_safe() == (1000.0, "wallet")
    x.fail["fetch_balance"] = ccxt.NetworkError("fake: down")
    assert await v.get_balance_safe() == (0.0, "unavailable")
    x.fail["fetch_balance"] = ccxt.NetworkError("fake: down")
    with pytest.raises(ConnectionError):
        await v.get_account_balance()


async def test_funding_reads_as_settlements_and_is_unknown_where_the_venue_has_none():
    v, x = _venue()
    x.funding = [{"symbol": "BTC/USDT:USDT", "amount": -0.75, "timestamp": 2000},
                 {"symbol": "BTC/USD:BTC", "amount": 9, "timestamp": 2000}]
    assert await v.get_transaction_log(start_time=1000) == [
        {"type": "SETTLEMENT", "symbol": "BTCUSDT", "change": "-0.75", "funding": "-0.75", "transactionTime": "2000"}]
    none, _ = _venue(Profile("fakex", "no funding"))
    with pytest.raises(VenueRejected, match="funding history"):
        await none.get_transaction_log(start_time=1000)


async def test_closed_pnl_records_come_from_the_venues_fills():
    v, x = _venue(price=100.0)
    await _long(v, x)                                                  # 5 contracts = 0.05 BTC at 100
    x.prices["BTC/USDT:USDT"] = 120.0
    await v.place_market_order("BTCUSDT", "Sell", "0.02", reduce_only=True)
    await v.place_market_order("BTCUSDT", "Sell", "0.03", reduce_only=True)
    rows = await v.get_closed_pnl(symbol="BTCUSDT", start_time=0, max_pages=2)
    closes = [r for r in rows if r["side"] == "Sell"]
    assert [r["closedSize"] for r in closes] == ["0.03", "0.02"]          # newest first
    assert [r["closedPnl"] for r in closes] == ["0.5", "0.3"]              # (120-100) x size, less the 0.1 fee
    assert closes[0]["avgExitPrice"] == "120"
    (opening,) = [r for r in rows if r["side"] == "Buy"]
    assert opening["closedPnl"] == "-0.1"                              # the relay ignores the opening side
    plain, _ = _venue(Profile("fakex", "no pnl in fills"))
    assert await plain.get_closed_pnl(symbol="BTCUSDT", start_time=0) == []


async def test_a_fill_without_its_pnl_leaves_the_record_incomplete():
    v, x = _venue(price=100.0)
    await _long(v, x)
    await v.place_market_order("BTCUSDT", "Sell", "0.05", reduce_only=True)
    x.trades[-1]["info"] = {}
    (close,) = [r for r in await v.get_closed_pnl(symbol="BTCUSDT", start_time=0) if r["side"] == "Sell"]
    assert close["closedPnl"] == ""


async def test_prepare_sets_one_way_mode():
    v, x = _venue()
    x.apiKey, x.secret = "k", "s"
    assert await v.prepare() == [] and x.hedged is False
    assert await v.prepare() == []                                     # already one-way: fine
    x.hedged = True
    x.fail["set_position_mode"] = ccxt.ExchangeError("fake: positions are open")
    (warning,) = await v.prepare()
    assert "one-way" in warning


async def test_without_keys_there_is_no_account_to_put_in_one_way_mode():
    v, x = _venue()
    assert await v.prepare() == [] and x.hedged is True and "set_position_mode" not in x.names()


async def test_the_breaker_opens_on_failures_but_safety_calls_get_through():
    from relay.util.circuit_breaker import CircuitBreakerOpen
    v, x = _venue()
    await _long(v, x)
    for _ in range(v.circuit_breaker.failure_threshold):
        x.fail["fetch_ticker"] = ccxt.ExchangeNotAvailable("fake: down")
        with pytest.raises(ConnectionError):
            await v.get_ticker("BTCUSDT")
    assert v.get_circuit_breaker_status()["is_open"]
    with pytest.raises(CircuitBreakerOpen):
        await v.place_market_order("BTCUSDT", "Buy", "0.01")
    await v.place_market_order("BTCUSDT", "Sell", "0.05", reduce_only=True)   # a close still goes out
    assert "BTC/USDT:USDT" not in x.positions


# ------------------------------------------------------------------ the relay's engine on top
async def test_the_engine_trades_live_through_the_connector(db):
    """The real execution engine, unchanged, on a venue that counts in contracts: entry, the stop on
    the exchange, the record in coins, a take-profit ladder, and the close."""
    from tests.test_engine import _engine, _no_sleep, _rows, _sig  # noqa: F401
    v, x = _venue(price=100.0)
    eng, _ = await _engine(db, v, dry_run=False)
    r = await eng.execute_entry(_sig())                                # 500 USDT at 100 = 5 BTC
    assert r["success"], r
    assert x.positions["BTC/USDT:USDT"]["contracts"] == 500           # 500 contracts of 0.01
    (pos,) = await _rows(db, "SELECT size, entry_price, stop_price, status FROM positions")
    assert (pos["size"], pos["entry_price"], pos["stop_price"], pos["status"]) == (5.0, 100.0, 95.0, "OPEN")
    (vp,) = await v.get_positions(symbol="BTCUSDT")
    assert vp["stopLoss"] == "95" and vp["size"] == "5"                # protected on the exchange
    assert x.leverage["BTC/USDT:USDT"] == 2
    (row,) = await eng.position_tracker.get_all_positions()
    x.prices["BTC/USDT:USDT"] = 110.0
    await eng.execute_exit(row)
    assert "BTC/USDT:USDT" not in x.positions
    (pos,) = await _rows(db, "SELECT status, exit_price FROM positions")
    assert (pos["status"], pos["exit_price"]) == ("CLOSED", 110.0)


async def test_the_engine_sees_a_stop_that_fired_on_the_exchange(db):
    from tests.test_engine import _engine, _no_sleep, _rows, _sig  # noqa: F401
    v, x = _venue(price=100.0)
    eng, _ = await _engine(db, v, dry_run=False)
    async with db.acquire() as c:                                      # as the runtime records it
        await c.execute("UPDATE accounts SET venue = 'fakex'")
    assert (await eng.execute_entry(_sig()))["success"]
    x.prices["BTC/USDT:USDT"] = 94.0
    x.trigger("BTC/USDT:USDT")
    await eng.position_tracker.sync_all_positions()
    (pos,) = await _rows(db, "SELECT status, exit_price, pnl, pnl_source FROM positions")
    assert pos["status"] == "CLOSED"
    assert (pos["exit_price"], pos["pnl_source"]) == (94.0, "venue_api")   # priced from the venue's fills
    assert pos["pnl"] == pytest.approx((94 - 100) * 5 - 0.1)


async def test_positions_of_another_venues_account_are_never_touched(db):
    """The default account is this venue's: a Bybit account's open position is not closed because
    the CCXT venue does not hold it."""
    from tests.test_engine import _engine, _no_sleep, _rows, _sig  # noqa: F401
    v, x = _venue(price=100.0)
    eng, _ = await _engine(db, v, dry_run=False)                      # the account says venue bybit
    assert (await eng.execute_entry(_sig()))["success"]
    x.positions.clear()                                                # not on this venue
    await eng.position_tracker.sync_all_positions()
    (pos,) = await _rows(db, "SELECT status FROM positions")
    assert pos["status"] == "OPEN"


# ------------------------------------------------------------------ MEXC (plan orders, raw fields)
async def test_mexc_stops_are_plan_orders_that_trigger_the_right_way_and_are_found_again():
    from relay.venue.profiles import PROFILES
    from tests.fake_ccxt import FakeMexc
    x = FakeMexc(price=100.0)
    v = CcxtVenue(x, PROFILES["mexc"])
    assert v.can_stop
    assert [r["symbol"] for r in await v.list_instruments()] == ["BTCUSDT"]     # ETH: apiAllowed false
    await _long(v, x)
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0))["retCode"] == 0
    (stop,) = [o for o in x.orders.values() if o.get("triggerPrice") == 95.0]
    assert stop["triggerType"] == 2 and stop["params"]["trend"] == 2 and stop["reduceOnly"]   # falls to 95
    (tp,) = [o for o in x.orders.values() if o.get("triggerPrice") == 120.0]
    assert tp["triggerType"] == 1 and "trend" not in tp["params"]                           # rises to 120
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"                                       # its tag read from info.externalOid
    x.calls.clear()
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 34040   # known: not placed again
    assert "create_order" not in x.names()
    await v.set_trading_stop("BTCUSDT", stop_loss=97.0)
    assert [o["triggerPrice"] for o in x.orders.values() if o["status"] == "open"] == [120.0, 97.0]
    x.prices["BTC/USDT:USDT"] = 96.0
    x.trigger("BTC/USDT:USDT")
    assert "BTC/USDT:USDT" not in x.positions


async def test_a_mexc_short_is_stopped_by_a_rising_price():
    from relay.venue.profiles import PROFILES
    from tests.fake_ccxt import FakeMexc
    x = FakeMexc(price=100.0)
    v = CcxtVenue(x, PROFILES["mexc"])
    await v.place_market_order("BTCUSDT", "Sell", "0.05")
    await v.set_trading_stop("BTCUSDT", stop_loss=105.0, take_profit=90.0)
    assert {o["triggerPrice"]: o["triggerType"] for o in x.orders.values() if o["status"] == "open"} == {105.0: 1,
                                                                                                         90.0: 2}
    (row,) = [r for r in await v.get_open_orders(symbol="BTCUSDT") if r["triggerPrice"] == "105"]
    assert row["side"] == "Buy" and row["reduceOnly"] and row["orderLinkId"].startswith("rl-")   # code 2


async def test_hyperliquid_market_orders_carry_a_reference_price():
    from relay.venue.profiles import PROFILES
    v, x = _venue(PROFILES["hyperliquid"], price=100.0)
    await v.place_market_order("SOLUSDT", "Buy", "1")
    (_, args, _) = [c for c in x.calls if c[0] == "create_order"][-1]
    assert args[4] == 100.0                                            # the venue caps slippage from it
    await v.set_trading_stop("SOLUSDT", stop_loss=95.0)
    (_, args, _) = [c for c in x.calls if c[0] == "create_order"][-1]
    assert args[4] == 95.0                                             # a stop: from its trigger price
    v2, x2 = _venue(price=100.0)
    await v2.place_market_order("BTCUSDT", "Buy", "0.01")
    assert [c for c in x2.calls if c[0] == "create_order"][-1][1][4] is None


async def test_the_engine_refuses_an_entry_with_a_stop_on_an_exchange_that_cannot_hold_one(db):
    from tests.test_engine import SETTINGS, _engine, _no_sleep, _sig  # noqa: F401
    v, x = _venue(price=100.0)
    x.has.pop("createStopLossOrder")
    assert not v.can_stop
    eng, _ = await _engine(db, v, dry_run=False, settings={**SETTINGS, "require_stop": False})
    r = await eng.execute_entry(_sig())
    assert r == {"success": False, "reason": "stop_unsupported"} and not x.positions
    r = await eng.execute_entry({**_sig(), "stop_price": None})
    assert r["success"], r                                             # no stop asked: it trades
    assert x.positions["BTC/USDT:USDT"]["contracts"] == 500


async def test_a_hyperliquid_close_gets_its_reference_price_through_an_open_breaker():
    from relay.util.circuit_breaker import CircuitBreakerOpen
    from relay.venue.profiles import PROFILES
    v, x = _venue(PROFILES["hyperliquid"], price=100.0)
    await v.place_market_order("SOLUSDT", "Buy", "2")
    for _ in range(v.circuit_breaker.failure_threshold):
        x.fail["fetch_ticker"] = ccxt.ExchangeNotAvailable("fake: down")
        with pytest.raises(ConnectionError):
            await v.get_ticker("SOLUSDT")
    with pytest.raises(CircuitBreakerOpen):
        await v.place_market_order("SOLUSDT", "Buy", "1")
    await v.place_market_order("SOLUSDT", "Sell", "2", reduce_only=True)
    assert "SOL/USDC:USDC" not in x.positions


def test_a_raw_field_fills_in_only_what_ccxt_left_unread():
    from relay.venue.profiles import PROFILES
    v = CcxtVenue(FakeExchange(), PROFILES["mexc"])
    read = {"clientOrderId": "rl-a", "side": "buy", "info": {"externalOid": "other", "side": "1"}}
    v._complete(read)
    assert read["clientOrderId"] == "rl-a"
    unread = {"clientOrderId": None, "side": "4", "info": {"externalOid": "rl-b", "side": "4"}}
    v._complete(unread)
    assert (unread["clientOrderId"], unread["side"], unread["reduceOnly"]) == ("rl-b", "sell", True)


async def test_a_stop_is_cancelled_by_its_id_although_the_venue_files_stops_apart():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (row,) = [r for r in await v.get_open_orders(symbol="BTCUSDT") if r["triggerPrice"]]
    await v.cancel_order("BTCUSDT", order_id=row["orderId"])
    assert x.orders[row["orderId"]]["status"] == "canceled"
    with pytest.raises(BenignOrderStateError):                         # gone either way: benign
        await v.cancel_order("BTCUSDT", order_id=row["orderId"])
    v2, x2 = _venue(Profile("fakex", "plain", trigger_list_params={}))
    with pytest.raises(BenignOrderStateError):
        await v2.cancel_order("BTCUSDT", order_id="nope")
    assert len([c for c in x2.calls if c[0] == "cancel_order"]) == 1  # no trigger retry without the flag


# ------------------------------------------------------------------ review round: MEXC and stopless venues
def _mexc(**kw):
    from relay.venue.profiles import PROFILES
    from tests.fake_ccxt import FakeMexc
    x = FakeMexc(price=100.0, **kw)
    return CcxtVenue(x, PROFILES["mexc"]), x


async def test_mexc_trigger_orders_live_seven_days_and_an_aged_stop_is_renewed_new_before_old():
    v, x = _mexc()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0)
    assert all(o["params"]["executeCycle"] == 2 for o in x.orders.values() if o.get("trigger"))
    (stop,) = [o for o in x.orders.values() if o.get("triggerPrice") == 95.0]
    stop["timestamp"] -= int(5.7 * 24 * 3600 * 1000)                 # past 80% of 7 days
    x.calls.clear()
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"                                       # renewed by the read, still protected
    assert [c[0] for c in x.calls if c[0] in ("create_order", "cancel_order")] == ["create_order", "cancel_order"]
    assert stop["status"] == "canceled"
    (fresh,) = [o for o in x.orders.values() if o["status"] == "open" and o.get("triggerPrice") == 95.0]
    assert fresh["id"] != stop["id"]
    x.calls.clear()
    await v.get_positions(symbol="BTCUSDT")
    assert "create_order" not in x.names()                             # once, not on every read


async def test_an_aged_stop_that_cannot_be_renewed_reads_as_missing_and_a_foreign_one_is_left_alone():
    v, x = _mexc()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (stop,) = [o for o in x.orders.values() if o.get("triggerPrice") == 95.0]
    stop["timestamp"] -= 6 * 24 * 3600 * 1000
    x.fail["create_order"] = ccxt.InvalidOrder("fake: refused")
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "" and stop["status"] == "open"            # the old one still rests; the audit re-arms
    v2, x2 = _mexc()
    await _long(v2, x2)
    await x2.create_order("BTC/USDT:USDT", "market", "sell", 5, None,
                          {"triggerPrice": 90.0, "triggerType": 2, "reduceOnly": True, "clientOrderId": "mine"})
    (own,) = [o for o in x2.orders.values() if o.get("triggerPrice") == 90.0]
    own["timestamp"] -= 6 * 24 * 3600 * 1000
    x2.calls.clear()
    await v2.get_positions(symbol="BTCUSDT")
    assert "create_order" not in x2.names() and own["status"] == "open"       # the owner's own: not renewed


async def test_mexc_cancel_all_reaches_the_plan_orders():
    v, x = _mexc()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0)
    await v.cancel_all_orders(symbol="BTCUSDT")
    assert not [o for o in x.orders.values() if o["status"] == "open"]


async def test_stops_are_refused_where_the_venue_does_not_give_the_tag_back():
    v, x = _mexc(echo=False)
    await _long(v, x)
    r = await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    assert r["retCode"] == -1 and "tag back" in r["retMsg"]
    assert not [o for o in x.orders.values() if o["status"] == "open"]          # the untagged stop was cancelled
    x.calls.clear()
    r = await v.set_trading_stop("BTCUSDT", stop_loss=96.0)
    assert r["retCode"] == -1 and "create_order" not in x.names()              # refused from then on
    v2, x2 = _mexc()
    await _long(v2, x2)
    assert (await v2.set_trading_stop("BTCUSDT", stop_loss=95.0))["retCode"] == 0
    assert v2._tags_echoed is True
    x2.calls.clear()
    await v2.set_trading_stop("BTCUSDT", stop_loss=96.0)
    assert x2.names().count("fetch_open_orders") == 1                          # checked once, not every time


async def test_a_venue_without_stop_orders_sends_no_take_profit_or_stop_at_all():
    v, x = _venue()
    x.has.pop("createStopLossOrder")
    await _long(v, x)
    for kw in ({"stop_loss": 95.0}, {"take_profit": 120.0}, {"trailing_stop": 2.0}):
        r = await v.set_trading_stop("BTCUSDT", **kw)
        assert r["retCode"] == -1 and "close at market" in r["retMsg"]
    assert x.names().count("create_order") == 1 and x.positions["BTC/USDT:USDT"]["contracts"] == 5   # the buy only


def test_mexc_leverage_and_margin_are_set_on_the_exchange_and_stopless_venues_refuse_trigger_settings():
    from relay.venue.factory import venue_problems
    from relay.venue.profiles import PROFILES
    has = {"setLeverage": True, "setMarginMode": True, "createTriggerOrder": True}
    refuse, _ = venue_problems(PROFILES["mexc"], has, {"leverage": 3, "isolated_margin": True})
    assert any(r.startswith("leverage is set") for r in refuse) and any("isolated_margin" in r for r in refuse)
    p = Profile("x", "x", verified=True)
    refuse, _ = venue_problems(p, {}, {"require_stop": False, "take_profit_pct": 3, "break_even_trigger_r": 1})
    assert sorted(r.split(" is set")[0] for r in refuse) == ["position_management.break_even_trigger_r",
                                                              "position_management.take_profit_pct"]
    assert venue_problems(p, {"createTriggerOrder": True}, {"take_profit_pct": 3})[0] == []


# ------------------------------------------------------------------ review round: where each venue files its stops
HTX_KINDS = {"sl": {"stopLoss": True}, "tp": {"takeProfit": True}, "ts": {"trailing": True}}


def _split(profile_name, **kw):
    from relay.venue.profiles import PROFILES
    from tests.fake_ccxt import FakeSplitLists
    x = FakeSplitLists(price=100.0, **kw)
    return CcxtVenue(x, PROFILES[profile_name]), x


async def test_htx_stops_and_take_profits_are_found_moved_and_cancelled_in_their_own_lists():
    v, x = _split("htx", kinds=HTX_KINDS)
    await _long(v, x)
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0))["retCode"] == 0
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"
    assert (await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0))["retCode"] == 34040
    await v.set_trading_stop("BTCUSDT", stop_loss=97.0, take_profit=125.0)
    resting = sorted(o.get("stopLossPrice") or o.get("takeProfitPrice") for o in x.orders.values()
                     if o["status"] == "open")
    assert resting == [97.0, 125.0]                                   # the old ones were found and cancelled
    tp = next(o for o in x.orders.values() if o["status"] == "open" and o.get("takeProfitPrice"))
    assert tp["clientOrderId"].isdigit() and len(tp["clientOrderId"]) == 18      # HTX: a numeric id
    await v.cancel_order("BTCUSDT", order_id=tp["id"])
    assert tp["status"] == "canceled"


async def test_okx_conditional_stops_are_listed_with_their_order_type():
    v, x = _split("okx", kinds={"sl": {"ordType": "conditional"}, "tp": {"ordType": "conditional"},
                                "ts": {"trailing": True}},
                  also={"sl": [{"trigger": True}], "tp": [{"trigger": True}]})
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"
    await v.set_trading_stop("BTCUSDT", stop_loss=97.0)
    assert [o["stopLossPrice"] for o in x.orders.values() if o["status"] == "open"] == [97.0]


@pytest.mark.parametrize("name", ["hyperliquid", "bingx"])
async def test_venues_that_list_stops_among_the_plain_orders(name):
    from relay.venue.profiles import PROFILES
    from tests.fake_ccxt import FakeSplitLists
    x = FakeSplitLists(kinds=HTX_KINDS, plain_lists_triggers=True, price=100.0)
    x.markets["BTC/USDC:USDC"] = {**x.markets["BTC/USDT:USDT"], "symbol": "BTC/USDC:USDC", "quote": "USDC",
                                  "settle": "USDC"}
    x.prices["BTC/USDC:USDC"] = 100.0
    v = CcxtVenue(x, PROFILES[name])
    await v.place_market_order("BTCUSDT", "Buy", "0.05")
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (p,) = await v.get_positions(symbol="BTCUSDT")
    assert p["stopLoss"] == "95"
    assert all(c[2].get("params") in ({}, None) for c in x.calls if c[0] == "fetch_open_orders")


async def test_cancel_all_sweeps_what_a_venue_without_cancel_all_leaves():
    v, x = _split("htx", kinds=HTX_KINDS, cancel_all=False)
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0)
    await v.place_limit_order("BTCUSDT", "Sell", "0.02", "130", reduce_only=True)
    await v.cancel_all_orders(symbol="BTCUSDT")
    assert not [o for o in x.orders.values() if o["status"] == "open"]


@pytest.mark.parametrize("link", ["rl-d7-0123456789abcd", "rl-d123456-ffffffffffffff", "rl-sl-0123456789ab",
                                  "rl-tp-abcdefabcdef", "rl-ts-000000000001"])
def test_htx_numeric_ids_keep_the_kind_and_the_deployment(link):
    from relay.venue.ccxt_venue import _tag_kind
    enc = encode_client_id(link, "int63")
    assert enc.isdigit() and len(enc) == 18 and int(enc) < 2**63
    back = decode_client_id(enc, "int63")
    assert encode_client_id(back, "int63") == enc                      # stable both ways
    assert _tag_kind(back) == _tag_kind(link)
    if link.startswith("rl-d"):
        assert back.split("-")[1] == link.split("-")[1]                # the deployment survives
    assert decode_client_id("12345", "int63") == "12345"               # a foreign id as it is


# ------------------------------------------------------------------ second review round
async def test_a_venue_that_drops_the_tag_counts_as_holding_no_stop_so_entries_are_refused_up_front(db):
    from tests.test_engine import SETTINGS, _engine, _no_sleep, _sig  # noqa: F401
    v, x = _mexc(echo=False)
    eng, _ = await _engine(db, v, dry_run=False, settings={**SETTINGS, "leverage": None})
    assert v.can_stop
    first = await eng.execute_entry(_sig())
    assert not first["success"] and not v.can_stop                     # found out at the first stop
    x.calls.clear()
    second = await eng.execute_entry(_sig())
    assert second == {"success": False, "reason": "stop_unsupported"} and "create_order" not in x.names()


async def test_a_plan_order_cancelled_as_a_plain_one_is_cancelled_where_it_is_filed():
    v, x = _venue()
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0)
    (stop,) = [o for o in x.orders.values() if o.get("trigger")]
    x.fail["cancel_order"] = ccxt.InvalidOrder("fake: not a plain order")    # MEXC's answer
    await v.cancel_order("BTCUSDT", order_id=stop["id"])
    assert stop["status"] == "canceled"


async def test_one_refused_cancel_stops_no_other_in_a_cancel_all():
    v, x = _split("htx", kinds=HTX_KINDS, cancel_all=False)
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0)
    stubborn = next(o for o in x.orders.values() if o.get("stopLossPrice"))
    real = v.cancel_order

    async def refuse_one(symbol, order_id=None, **kw):
        if order_id == stubborn["id"]:
            raise VenueRejected("Venue rejected (htx): error: fake")
        return await real(symbol, order_id=order_id, **kw)
    v.cancel_order = refuse_one
    await v.cancel_all_orders(symbol="BTCUSDT")
    assert [o["id"] for o in x.orders.values() if o["status"] == "open"] == [stubborn["id"]]


async def test_bitget_stops_are_cancelled_with_their_plan_type():
    from relay.venue.profiles import PROFILES
    from tests.fake_ccxt import FakeSplitLists
    x = FakeSplitLists(kinds={"sl": {"planType": "profit_loss"}, "tp": {"planType": "profit_loss"},
                              "ts": {"trailing": True}},
                       cancel_with={"sl": {"trigger": True, "planType": "pos_loss"},
                                    "tp": {"trigger": True, "planType": "pos_profit"}}, price=100.0)
    v = CcxtVenue(x, PROFILES["bitget"])
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", stop_loss=95.0, take_profit=120.0)
    await v.set_trading_stop("BTCUSDT", stop_loss=97.0, take_profit=125.0)
    assert sorted(o.get("stopLossPrice") or o.get("takeProfitPrice") for o in x.orders.values()
                  if o["status"] == "open") == [97.0, 125.0]


def test_binance_may_list_every_markets_open_orders_for_the_emergency_cancel_all():
    from relay.venue.factory import build_ccxt_venue
    v = build_ccxt_venue("binance", "testnet", paper=True, environ={})
    assert v.x.options["warnOnFetchOpenOrdersWithoutSymbol"] is False


async def test_a_list_that_refuses_the_order_outright_is_passed_over_for_the_next():
    v, x = _split("htx", kinds=HTX_KINDS)
    await _long(v, x)
    await v.set_trading_stop("BTCUSDT", take_profit=120.0)
    (tp,) = [o for o in x.orders.values() if o["status"] == "open"]
    real = x.cancel_order

    async def stop_list_refuses(id, symbol=None, params=None):
        if params == {"stopLoss": True}:
            raise ccxt.InvalidOrder("fake: not a stop-loss order")         # a refusal, not "not found"
        return await real(id, symbol, params)
    x.cancel_order = stop_list_refuses
    await v.cancel_order("BTCUSDT", order_id=tp["id"])
    assert tp["status"] == "canceled"
