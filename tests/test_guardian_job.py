"""Guardian loop: per-account keying, persisted halts, flatten-all, and the live/dry-run rule."""
import json
from datetime import UTC, datetime

import pytest

import relay.safety.guardian_job as gj
from relay.db.models import Account
from relay.safety.account_dd_guardian import AccountDDGuardian, GuardianConfig

# Example limits for the tests only: 4% daily, 8% max, no buffer -> max floor 9,200 on 10,000.
CFG = GuardianConfig(initial_balance=10_000, daily_dd_pct=4, max_dd_pct=8)


@pytest.fixture(autouse=True)
def _clean_registry():
    gj._guardians.clear()
    yield
    gj._guardians.clear()


class _Acct:
    def __init__(self, name, venue="bybit", credentials_ref=None):
        self.name, self.venue, self.credentials_ref = name, venue, credentials_ref


# ---- per-account keying ------------------------------------------------------------

def test_guardian_key_default_bybit_account():
    assert gj.guardian_key(_Acct("main", "bybit", None)) == "default"


def test_guardian_key_keyed_bybit_account_is_its_own():
    assert gj.guardian_key(_Acct("sub-account-1", "bybit", "SUB_ACCOUNT_1")) == "sub-account-1"


def test_guardian_key_two_bybit_accounts_never_collide():
    assert gj.guardian_key(_Acct("main")) != gj.guardian_key(_Acct("sub-account-1", "bybit", "SUB_ACCOUNT_1"))


def test_guardian_key_other_venue_uses_its_name():
    assert gj.guardian_key(_Acct("hl-main", "hyperliquid", None)) == "hl-main"


# ---- persisted state -----------------------------------------------------------------

def test_state_roundtrip_persists_max_dd_halt():
    g = AccountDDGuardian(CFG)
    g.observe(9_000.0, datetime(2026, 1, 20, 12, 0, tzinfo=UTC))
    assert g.halted and g.halt_type == "max_dd"
    g2 = AccountDDGuardian(CFG)  # a restart
    g2.load_state(g.to_state())
    assert g2.halted and g2.halt_type == "max_dd"


def test_get_guardian_account_loads_persisted_halt():
    src = AccountDDGuardian(CFG)
    src.observe(9_000.0, datetime(2026, 1, 20, 12, 0, tzinfo=UTC))
    g = gj._get_guardian_account("sub-account-1", CFG, persisted_state=src.to_state(), account_id=42)
    assert g.halted and g._account_id == 42
    assert gj.entries_allowed("sub-account-1") is False
    assert gj.entries_allowed("default", guardian_configured=False) is True  # another account is unaffected


def test_corrupt_state_fails_closed_and_empty_does_not_halt():
    assert gj.load_persisted_state("{not valid json") == {"halted": True, "halt_type": "max_dd"}
    assert gj.load_persisted_state("{}") == {} and gj.load_persisted_state(None) == {}


async def test_ensure_armed_accounts_loads_persisted_halt(db):
    async with db.session() as s:
        s.add(Account(name="main", guardian_state=json.dumps({"halted": True, "halt_type": "max_dd"})))
        s.add(Account(name="unguarded"))
        await s.commit()
    await gj.ensure_armed_accounts(db, {"main": CFG})
    assert gj.entries_allowed("default") is False                         # halted from tick 0
    assert set(gj._guardians) == {"default"}                              # only configured accounts arm


async def test_ensure_armed_accounts_fails_closed_on_corrupt_state(db):
    async with db.session() as s:
        s.add(Account(name="main", guardian_state="{oops"))
        await s.commit()
    await gj.ensure_armed_accounts(db, {"main": CFG})
    assert gj.entries_allowed("default") is False


def test_entries_allowed_fails_closed_only_when_a_guardian_is_configured():
    assert gj.entries_allowed("sub-account-1") is False                   # configured, not armed
    assert gj.entries_allowed("sub-account-1", guardian_configured=False) is True
    g = gj._get_guardian_account("sub-account-1", CFG)
    assert gj.entries_allowed("sub-account-1") is True
    g.load_state({"halted": True, "halt_type": "max_dd"})
    assert gj.entries_allowed("sub-account-1") is False


# ---- flatten_all -------------------------------------------------------------------

async def test_flatten_all_closes_every_open_position():
    calls = []

    async def close_fn(symbol, side, qty):
        calls.append((symbol, side, qty))
        return {"success": True}

    positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.5"},
                 {"symbol": "ETHUSDT", "side": "Sell", "size": "3"},
                 {"symbol": "SOLUSDT", "side": "Buy", "size": "0"}]
    assert (await gj.flatten_all(positions, close_fn, "guardian:max_dd"))["closed"] == 2
    assert calls == [("BTCUSDT", "long", 0.5), ("ETHUSDT", "short", 3.0)]


async def test_flatten_all_survives_a_failing_close():
    async def close_fn(symbol, side, qty):
        if symbol == "A":
            raise RuntimeError("exchange down")
        return {"success": True}

    positions = [{"symbol": "A", "side": "Buy", "size": "1"}, {"symbol": "B", "side": "Sell", "size": "1"}]
    assert await gj.flatten_all(positions, close_fn, "r") == {"closed": 1, "failed": ["A"], "simulated": 0}


async def test_a_refused_close_is_never_counted_as_closed():
    """close_position_fast reports a refusal in its result instead of raising."""
    async def close_fn(symbol, side, qty):
        return {"success": False, "error": "rate limited"}

    out = await gj.flatten_all([{"symbol": "A", "side": "Buy", "size": "1"}], close_fn, "r")
    assert out == {"closed": 0, "failed": ["A"], "simulated": 0}


def test_a_settings_change_keeps_the_halt():
    """Rebuilding the guardian for new limits must not quietly re-arm a halted account."""
    from relay.safety.account_dd_guardian import GuardianConfig as GC
    g = gj._get_guardian_account("rebuilt", CFG)
    g.observe(9_000.0, datetime(2026, 1, 20, 12, 0, tzinfo=UTC))                # max_dd halt
    assert g.halted
    g2 = gj._get_guardian_account("rebuilt", GC(initial_balance=10_000, daily_dd_pct=4, max_dd_pct=9))
    assert g2 is not g and g2.halted and g2.halt_type == "max_dd"


# ---- run_guardian_check_account ----------------------------------------------------

class _Client:
    def __init__(self, equity, positions=(), cancel="ok", breaker_open=False):
        self._equity = equity
        self._positions = positions if isinstance(positions, str) else list(positions)
        self.seq = []
        self._cancel = cancel
        if breaker_open:
            self.circuit_breaker = _Breaker()

    def __getattr__(self, name):  # cancel_all_orders exists unless cancel="missing"
        if name == "cancel_all_orders" and self._cancel != "missing":
            return self._cancel_all
        raise AttributeError(name)

    def _gate(self):
        b = self.__dict__.get("circuit_breaker")
        if b is not None and b.open:
            raise RuntimeError("CircuitBreakerOpen")

    async def get_account_balance(self, bypass_breaker=False):
        if not bypass_breaker:
            self._gate()
        return self._equity

    async def get_positions(self, category=None, settle_coin=None, bypass_breaker=False):
        if not bypass_breaker:
            self._gate()
        if self._positions == "unreadable":
            raise RuntimeError("venue down")
        return self._positions

    async def _cancel_all(self, category=None, settle_coin=None):
        self.seq.append(("cancel_all", category, settle_coin))
        if self._cancel == "raises":
            raise RuntimeError("exchange unreachable")

    async def place_market_order(self, symbol, side, qty, category=None, reduce_only=None):
        if not reduce_only:
            self._gate()                       # like the real client: reduce-only passes an open breaker
        self.seq.append(("close", symbol, side, qty, reduce_only))


class _Breaker:
    def __init__(self):
        self.open, self.resets = True, 0

    def reset(self):
        self.resets += 1
        self.open = False


class _Exec:
    def __init__(self):
        self.closed = []

    async def close_position_fast(self, symbol, side, quantity, client=None, simulate=None):
        self.closed.append((symbol, side, quantity, client, simulate))


_POS = [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.5"}, {"symbol": "ETHUSDT", "side": "Sell", "size": "2"}]


async def _account(db, name="main"):
    async with db.session() as s:
        a = Account(name=name)
        s.add(a)
        await s.commit()
        return a.id


async def _saved_state(db, account_id):
    async with db.session() as s:
        return json.loads((await s.get(Account, account_id)).guardian_state)


async def test_live_breach_cancels_first_then_flattens_reduce_only(db):
    """THE ORDERING IS THE ASSERTION: cancelling after the flatten would leave a window in which a
    resting entry order fills and re-opens a leg the flatten just closed."""
    acc = await _account(db)
    client = _Client(9_000, _POS)
    res = await gj.run_guardian_check_account(db, client, CFG, "default", account_id=acc, live=True)
    assert res["breach"] == "max_dd" and res["closed"] == 2 and res["live"] is True
    assert client.seq[0] == ("cancel_all", "linear", "USDT")
    assert client.seq[1:] == [("close", "BTCUSDT", "Sell", "0.5", True), ("close", "ETHUSDT", "Buy", "2.0", True)]
    assert gj.entries_allowed("default") is False
    assert (await _saved_state(db, acc))["halt_type"] == "max_dd"   # survives a restart


async def test_dry_run_breach_halts_and_persists_but_sends_nothing(db):
    """No live deployment on the account: the relay opened nothing real, so the flatten must not
    touch positions a person opened by hand. Entries are still halted."""
    acc = await _account(db)
    client, ex = _Client(9_000, _POS), _Exec()
    res = await gj.run_guardian_check_account(db, client, CFG, "default", order_executor=ex, account_id=acc)
    assert res["breach"] == "max_dd" and res["live"] is False
    assert client.seq == []                                             # no cancel, no raw close
    assert ex.closed == []                                              # the executor is not even called
    assert gj.entries_allowed("default") is False
    assert (await _saved_state(db, acc))["halted"] is True


async def test_dry_run_raw_fallback_places_nothing(db):
    client = _Client(9_000, _POS)
    res = await gj.run_guardian_check_account(db, client, CFG, "default", live=False)
    assert res["simulated"] == 2 and res["closed"] == 0 and client.seq == []   # simulated, nothing sent


async def test_live_flatten_through_executor_forces_live_on_the_accounts_own_client(db):
    client, ex = _Client(9_000, _POS[:1]), _Exec()
    await gj.run_guardian_check_account(db, client, CFG, "sub-account-1", order_executor=ex, live=True)
    assert ex.closed == [("BTCUSDT", "long", 0.5, client, False)]      # simulate=False: a real close


async def test_the_guardian_reads_and_flattens_through_an_open_breaker_without_resetting_it(db):
    """A correlated rate-limit + drawdown incident can open the breaker. The guardian's reads pass it
    (they add no risk) instead of resetting it, which would also let new orders through."""
    client = _Client(9_000, _POS[:1], breaker_open=True)
    res = await gj.run_guardian_check_account(db, client, CFG, "default", live=True)
    assert client.circuit_breaker.resets == 0 and client.circuit_breaker.open
    assert res["breach"] == "max_dd" and ("close", "BTCUSDT", "Sell", "0.5", True) in client.seq


async def test_unreadable_positions_during_a_breach_are_reported_not_a_clean_flatten(db):
    client = _Client(9_000, "unreadable")
    res = await gj.run_guardian_check_account(db, client, CFG, "default", live=True)
    assert res["breach"] == "max_dd" and res["positions_unreadable"] is True


async def test_breach_still_flattens_when_cancel_all_fails(db):
    client = _Client(9_000, _POS[:1], cancel="raises")
    res = await gj.run_guardian_check_account(db, client, CFG, "default", live=True)
    assert res["closed"] == 1 and client.seq[-1][0] == "close"


async def test_breach_flattens_when_client_cannot_cancel_at_all(db):
    client = _Client(9_000, _POS[:1], cancel="missing")
    res = await gj.run_guardian_check_account(db, client, CFG, "default", live=True)
    assert res["closed"] == 1


async def test_breach_on_one_account_leaves_others_alone(db):
    gj._get_guardian_account("default", CFG)
    await gj.run_guardian_check_account(db, _Client(9_000, _POS), CFG, "sub-account-1", live=True)
    assert gj.entries_allowed("sub-account-1") is False
    assert gj.entries_allowed("default") is True


async def test_healthy_tick_is_ok_and_persists_new_peak(db):
    acc = await _account(db)
    res = await gj.run_guardian_check_account(db, _Client(10_050), CFG, "default", account_id=acc)
    assert res["ok"] is True and res["halted"] is False
    assert (await _saved_state(db, acc))["intraday_peak"] == 10_050


async def test_equity_read_failure_is_reported_not_a_breach(db):
    class _Down(_Client):
        async def get_account_balance(self):
            raise RuntimeError("timeout")

    res = await gj.run_guardian_check_account(db, _Down(0), CFG, "default", live=True)
    assert res == {"error": "equity_fetch"} and gj.entries_allowed("default") is True


async def test_daily_reset_rearms_daily_not_max(db):
    acc = await _account(db)
    g = gj._get_guardian_account("default", CFG, account_id=acc)
    g.load_state({"halted": True, "halt_type": "max_dd"})
    out = await gj.guardian_daily_reset(db)
    assert out["rearmed"] == {"default": False} and g.halted              # max-DD is lifetime
    g.load_state({"halted": True, "halt_type": "daily_dd"})
    out = await gj.guardian_daily_reset(db)
    assert out["rearmed"] == {"default": True} and not g.halted
    assert (await _saved_state(db, acc))["halted"] is False


async def test_daily_reset_with_nothing_armed(db):
    assert await gj.guardian_daily_reset(db) == {"skipped": "not_armed"}
