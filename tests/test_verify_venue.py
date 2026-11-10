"""relay verify-venue: the steps on in-memory exchanges, the cleanup after a failure, and the refusals."""
import ccxt
import pytest

from relay import cli
from relay.config import settings
from relay.venue.ccxt_spot import CcxtSpotVenue
from relay.venue.ccxt_venue import CcxtVenue
from relay.venue.profiles import PROFILES, Capabilities, Profile
from relay.venue.verify import passed, render, verify_venue
from tests.fake_ccxt import FakeExchange, FakeMexc, FakeSpot

PROFILE = Profile("fakex", "a fake exchange", realized_pnl_field="pnl",
                  capabilities=Capabilities(closed_pnl=True, funding_history=True))


async def _run(venue, **kw):
    async def rebuild():
        return type(venue)(venue.x, venue.profile)
    args = {"symbol": "BTCUSDT", "notional": 500.0, "stop_pct": 4.0, **kw}
    return await verify_venue(venue, args["symbol"], args["notional"], args["stop_pct"], rebuild,
                              can_ladder=venue.profile.capabilities.reduce_only_limit and not venue.spot)


def _by_name(steps):
    return {s.name: s for s in steps}


def _relay_orders_open(x):
    return [o for o in x.orders.values() if o["status"] == "open"]


async def test_every_step_passes_on_a_working_perpetual_connector():
    x = FakeExchange(price=100.0)
    x.funding.append({"symbol": "BTC/USDT:USDT", "amount": -0.1, "timestamp": 10**13, "id": "f1"})
    steps = await _run(CcxtVenue(x, PROFILE))
    assert passed(steps), render(steps)
    assert [s.name for s in steps] == ["read", "open", "stop", "move", "same", "ladder", "restart", "close",
                                       "records", "funding"]
    s = _by_name(steps)
    assert "order 5 BTCUSDT" in s["read"].detail and s["stop"].detail == "stop at 96.0"
    assert s["move"].detail == "moved to 98.0, no second stop resting" and "PnL per fill given" in s["records"].detail
    assert not x.positions and not _relay_orders_open(x)


async def test_spot_sells_half_and_the_stop_comes_back_on_the_rest():
    x = FakeSpot(price=100.0)
    steps = await _run(CcxtSpotVenue(x, Profile("fakex", "spot")))
    assert passed(steps), render(steps)
    s = _by_name(steps)
    assert s["partial sell"].ok and "the stop is back on 2.5" in s["partial sell"].detail
    assert "ladder" not in s and s["funding"].detail == "readable (0 entries in the window)"
    assert x.total.get("BTC", 0) == 0 and not _relay_orders_open(x)


async def test_mexc_passes_through_its_plan_orders():
    x = FakeMexc(price=100.0)
    steps = await _run(CcxtVenue(x, PROFILES["mexc"]))
    assert passed(steps), render(steps)
    assert not x.positions and not _relay_orders_open(x)


async def test_a_failed_step_skips_the_rest_and_the_position_is_closed_anyway():
    x = FakeExchange(price=100.0)
    v = CcxtVenue(x, PROFILE)
    real = v.set_trading_stop

    async def refused(*a, **kw):
        if kw.get("stop_loss"):
            return {"retCode": -1, "retMsg": "fake: refused"}
        return await real(*a, **kw)
    v.set_trading_stop = refused
    steps = await _run(v)
    s = _by_name(steps)
    assert s["open"].ok and s["stop"].ok is False and "refused" in s["stop"].detail
    assert all(st.ok is None for st in steps if st.name in ("move", "same", "ladder", "restart", "close"))
    assert s["cleanup"].ok and not x.positions and not _relay_orders_open(x)
    assert not passed(steps)


async def test_a_cleanup_that_fails_says_so_loudly():
    x = FakeExchange(price=100.0)
    v = CcxtVenue(x, PROFILE)

    async def broken(*a, **kw):
        raise RuntimeError("fake: stop endpoint down")
    v.set_trading_stop = broken
    real_place = v.place_market_order

    async def no_close(symbol, side, qty, **kw):
        if kw.get("reduce_only"):
            raise ccxt.ExchangeNotAvailable("fake: down")
        return await real_place(symbol, side, qty, **kw)
    v.place_market_order = no_close
    steps = await _run(v)
    s = _by_name(steps)
    assert s["cleanup"].ok is False and "COULD NOT CLOSE" in s["cleanup"].detail and "BTCUSDT" in s["cleanup"].detail


async def test_a_symbol_that_already_holds_a_position_is_not_touched():
    x = FakeExchange(price=100.0)
    v = CcxtVenue(x, PROFILE)
    await v.place_market_order("BTCUSDT", "Buy", "1")
    before = len(x.calls)
    steps = await _run(v)
    s = _by_name(steps)
    assert s["read"].ok is False and "already holds a position" in s["read"].detail
    assert "cleanup" not in s and x.positions["BTC/USDT:USDT"]["contracts"] == 100
    assert "create_order" not in [c[0] for c in x.calls[before:]]


async def test_a_size_below_the_exchanges_minimum_is_refused_before_any_order():
    x = FakeExchange(price=100.0)
    steps = await _run(CcxtVenue(x, PROFILE), notional=0.5)          # 0.005 BTC, below 1 contract (0.01)
    assert _by_name(steps)["read"].ok is False and "raise --notional" in _by_name(steps)["read"].detail
    assert "create_order" not in x.names()


async def test_an_exchange_without_stops_skips_the_stop_steps():
    x = FakeExchange(price=100.0)
    x.has.pop("createStopLossOrder")
    steps = await _run(CcxtVenue(x, PROFILE))
    s = _by_name(steps)
    assert [s[n].ok for n in ("stop", "move", "same")] == [None, None, None]
    assert s["close"].ok and passed(steps)


async def test_an_unreadable_balance_stops_the_check_before_it_trades():
    x = FakeExchange(price=100.0)
    x.fail["fetch_balance"] = ccxt.AuthenticationError("fake: bad key")
    steps = await _run(CcxtVenue(x, PROFILE))
    assert _by_name(steps)["read"].ok is False and "keys" in _by_name(steps)["read"].detail
    assert "create_order" not in x.names()


# ------------------------------------------------------------------ the command
def _deployment(tmp_path, monkeypatch, text):
    (tmp_path / "deployments").mkdir(exist_ok=True)
    (tmp_path / "deployments" / "main.yaml").write_text(text)
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))


ARGS = ["verify-venue", "--symbol", "btcusdt", "--notional", "500", "--stop-pct", "4"]


@pytest.mark.parametrize("text, said", [
    ("venue:\n  exchange: okx\n  environment: live\n", "never runs on a live account"),
    ("dry_run: true\n", "checks a CCXT exchange"),
])
def test_the_command_refuses_live_and_the_native_connector(tmp_path, monkeypatch, capsys, text, said):
    from relay.venue import factory
    monkeypatch.setattr(factory, "build_ccxt_venue", lambda *a, **k: pytest.fail("built a venue"))
    _deployment(tmp_path, monkeypatch, text)
    assert cli.main([*ARGS, "--confirm"]) == 1
    assert said in capsys.readouterr().out


def test_without_confirm_nothing_is_built_or_sent(tmp_path, monkeypatch, capsys):
    from relay.venue import factory
    monkeypatch.setattr(factory, "build_ccxt_venue", lambda *a, **k: pytest.fail("built a venue"))
    _deployment(tmp_path, monkeypatch, "venue:\n  exchange: okx\n  environment: testnet\n")
    assert cli.main(ARGS) == 0
    out = capsys.readouterr().out
    assert "would trade on okx (testnet)" in out and "Nothing was sent" in out


def test_the_command_runs_the_steps_with_keys_required(tmp_path, monkeypatch, capsys):
    from relay.venue import factory
    x = FakeExchange(price=100.0)
    built = []

    def build(exchange, environment, *, paper, market="perpetual", environ=None):
        built.append((exchange, environment, paper, market))
        return CcxtVenue(x, PROFILE)
    monkeypatch.setattr(factory, "build_ccxt_venue", build)
    _deployment(tmp_path, monkeypatch, "venue:\n  exchange: okx\n  environment: testnet\n")
    assert cli.main([*ARGS, "--confirm"]) == 0
    out = capsys.readouterr().out
    assert built[0] == ("okx", "testnet", False, "perpetual") and len(built) == 2     # and the restart's
    assert "PASS  close" in out and "RESULT: all steps passed" in out
    x.fail["fetch_balance"] = ccxt.AuthenticationError("fake: bad key")
    assert cli.main([*ARGS, "--confirm"]) == 1
    assert "RESULT: FAILED" in capsys.readouterr().out


def test_nonpositive_sizes_are_refused(tmp_path, monkeypatch, capsys):
    _deployment(tmp_path, monkeypatch, "venue:\n  exchange: okx\n  environment: testnet\n")
    assert cli.main(["verify-venue", "--symbol", "BTCUSDT", "--notional", "0", "--stop-pct", "4", "--confirm"]) == 1


# ------------------------------------------------------------------ every check fails a connector that breaks it
class _Broken(CcxtVenue):
    """A connector with one defect, named by `defect`."""
    defect = ""

    async def get_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        row = await super().get_order(symbol, order_id=order_id, order_link_id=order_link_id)
        if row and self.defect == "unfilled" and row["side"] == "Buy":
            row = {**row, "orderStatus": "New"}
        if row and self.defect == "ladder" and row["orderType"] == "Limit":
            row = {**row, "orderStatus": "Cancelled"}
        return row

    async def get_positions(self, *a, **kw):
        rows = await super().get_positions(*a, **kw)
        if self.defect in ("no stop shown", "restart"):
            rows = [{**r, "stopLoss": ""} for r in rows]
        return rows

    async def _cancel_all(self, m, orders):
        if self.defect == "old stop stays":
            return {"retCode": 0, "retMsg": "OK"}
        return await super()._cancel_all(m, orders)

    async def set_trading_stop(self, symbol, stop_loss=None, *a, **kw):
        r = await super().set_trading_stop(symbol, stop_loss, *a, **kw)
        return {"retCode": 0, "retMsg": "OK"} if self.defect == "same" and r["retCode"] == 34040 else r

    async def cancel_order(self, symbol, order_id=None, order_link_id=None, category="linear"):
        if self.defect == "leftover" and not self.x.positions:          # after the close, nothing is cancelled
            return {"orderId": order_id}
        return await super().cancel_order(symbol, order_id=order_id, order_link_id=order_link_id)


@pytest.mark.parametrize("defect, step", [
    ("unfilled", "open"), ("no stop shown", "stop"), ("old stop stays", "move"), ("same", "same"),
    ("ladder", "ladder"), ("restart", "restart"), ("leftover", "close"),
])
async def test_each_check_fails_a_connector_with_that_defect(defect, step):
    x = FakeExchange(price=100.0)
    v = _Broken(x, PROFILE)
    v.defect = "" if defect == "restart" else defect

    async def rebuild():
        fresh = _Broken(x, PROFILE)
        fresh.defect = defect
        return fresh
    steps = await verify_venue(v, "BTCUSDT", 500.0, 4.0, rebuild)
    failed = [s for s in steps if s.ok is False and s.name != "cleanup"]
    assert [s.name for s in failed] == [step], render(steps)
    assert not passed(steps)


async def test_a_size_between_the_lot_step_and_the_minimum_order_is_refused():
    x = FakeExchange(price=100.0)
    x.markets["BTC/USDT:USDT"]["limits"]["amount"]["min"] = 5          # 5 contracts = 0.05 BTC
    steps = await _run(CcxtVenue(x, PROFILE), notional=3.0)            # 0.03 BTC: three steps, below 0.05
    assert _by_name(steps)["read"].ok is False and "below the minimum order of 0.05" in _by_name(steps)["read"].detail


async def test_a_spot_partial_sell_that_loses_the_stop_fails():
    x = FakeSpot(price=100.0)
    v = CcxtSpotVenue(x, Profile("fakex", "spot"))

    async def lost(*a):
        return None
    v._restore_stop = lost
    steps = await _run(v)
    assert _by_name(steps)["partial sell"].ok is False and "0 stops" in _by_name(steps)["partial sell"].detail


async def test_a_buy_whose_answer_was_lost_is_still_cleaned_up():
    x = FakeExchange(price=100.0)
    v = CcxtVenue(x, PROFILE)
    real = v.place_market_order

    async def lost(symbol, side, qty, **kw):
        await real(symbol, side, qty, **kw)
        if not kw.get("reduce_only"):
            raise ConnectionError("fake: answer lost")
        return {"orderId": "x"}
    v.place_market_order = lost
    steps = await _run(v)
    assert _by_name(steps)["open"].ok is False and _by_name(steps)["cleanup"].ok and not x.positions
