"""Paper without exchange keys: the relay reads prices and market rules only, never the account
(it would only fail, loudly, every minute) - and says so if live positions are recorded anyway."""
from datetime import UTC, datetime

from relay.venue.ccxt_venue import CcxtVenue
from relay.venue.profiles import Profile
from tests.fake_ccxt import FakeExchange
from tests.test_engine import SETTINGS, _engine, _no_sleep, _real_sleep, _sig  # noqa: F401


def _keyless():
    x = FakeExchange(price=100.0)
    return CcxtVenue(x, Profile("fakex", "fake")), x


def test_a_venue_knows_whether_it_was_given_keys():
    v, x = _keyless()
    assert not v.has_keys
    x.apiKey = "k"
    assert not v.has_keys                                              # a key without its secret
    x.secret = "s"
    assert v.has_keys
    w = FakeExchange(price=100.0)
    wallet = CcxtVenue(w, Profile("fakew", "fake", credentials=("walletAddress", "privateKey")))
    w.walletAddress = "0xabc"
    assert not wallet.has_keys
    w.privateKey = "p"
    assert wallet.has_keys


async def test_paper_without_keys_never_reads_the_account_but_still_trades_on_paper(db):
    v, x = _keyless()
    eng, _ = await _engine(db, v, dry_run=True)
    assert not eng.account_readable()
    await eng.start()
    try:
        assert not {"fetch_positions", "fetch_balance", "fetch_my_trades"} & set(x.names())   # start and loops
        r = await eng.execute_entry(_sig())
        assert r["success"], r                                         # a paper entry still works
    finally:
        await eng.stop()


async def test_live_positions_recorded_without_keys_are_said_loudly(db):
    from loguru import logger

    from relay.db.models import Position
    v, _ = _keyless()
    eng, dep = await _engine(db, v, dry_run=True)
    async with db.session() as s:
        s.add(Position(deployment_id=dep, symbol="BTCUSDT", side="LONG", size=1, entry_price=100, status="OPEN",
                       dry_run=False, opened_at=datetime.now(UTC)))
        await s.commit()
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="CRITICAL")
    try:
        assert await eng._live_rows_without_keys() == 1
    finally:
        logger.remove(sink)
    assert any("1 live position(s) are recorded, but there are no exchange keys" in line for line in lines)


async def test_a_client_that_does_not_say_is_read_as_before(db):
    from tests.test_engine import FakeBybit
    eng, _ = await _engine(db, FakeBybit(), dry_run=True)
    assert eng.account_readable()


async def test_the_native_bybit_client_is_read_by_its_key(db):
    from tests.test_engine import FakeBybit
    fake = FakeBybit()
    eng, _ = await _engine(db, fake, dry_run=True)
    fake.api_key = ""                                                  # BYBIT_API_KEY not set
    assert not eng.account_readable()
    fake.api_key = "k"
    assert eng.account_readable()


async def test_live_without_keys_refuses_to_start(db):
    import pytest

    from tests.test_engine import FakeBybit
    fake = FakeBybit()
    fake.api_key = ""
    eng, _ = await _engine(db, fake, dry_run=False)
    with pytest.raises(RuntimeError, match="needs the venue's exchange keys"):
        await eng.start()
    assert not eng._running and not getattr(eng, "_tasks", [])


async def test_a_live_ccxt_venue_without_keys_refuses_to_start_too(db):
    import pytest
    v, x = _keyless()
    eng, _ = await _engine(db, v, dry_run=False)
    with pytest.raises(RuntimeError, match="needs the venue's exchange keys"):
        await eng.start()
    assert not {"fetch_positions", "fetch_balance"} & set(x.names())


async def test_unwatched_live_positions_are_an_event_once_per_change(db):
    from relay.db.models import Position
    v, _ = _keyless()
    eng, dep = await _engine(db, v, dry_run=True)
    seen = []

    class Events:
        async def record(self, kind, severity, message, data=None, position_id=None):
            seen.append((kind, severity, data))
    eng.events = Events()
    assert await eng._live_rows_without_keys() == 0 and seen == []
    async with db.session() as s:
        s.add(Position(deployment_id=dep, symbol="BTCUSDT", side="LONG", size=1, entry_price=100, status="OPEN",
                       dry_run=False, opened_at=datetime.now(UTC)))
        await s.commit()
    await eng._live_rows_without_keys()
    await eng._live_rows_without_keys()                                # the same count: said once
    assert seen == [("position.unwatched", "critical", {"count": 1})]
    async with db.session() as s:
        s.add(Position(deployment_id=dep, symbol="ETHUSDT", side="LONG", size=1, entry_price=100, status="OPEN",
                       dry_run=False, opened_at=datetime.now(UTC)))
        await s.commit()
    await eng._live_rows_without_keys()
    assert seen[-1] == ("position.unwatched", "critical", {"count": 2})


async def test_the_sync_loop_reads_no_account_without_keys(db):
    import asyncio
    v, x = _keyless()
    eng, _ = await _engine(db, v, dry_run=True)
    eng._running = True
    rounds = []
    real = eng._live_rows_without_keys

    async def counted():
        rounds.append(1)
        return await real()
    eng._live_rows_without_keys = counted
    task = asyncio.create_task(eng._position_sync_loop())
    for _ in range(200):
        if rounds:
            break
        await _real_sleep(0.01)                  # a real wait: the fixture makes short sleeps instant
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)                 # let go of its connection
    assert rounds, "the loop never reached its keyless round"
    assert "fetch_positions" not in x.names()


async def test_only_live_rows_count_as_unwatched(db):
    from relay.db.models import Position
    v, _ = _keyless()
    eng, dep = await _engine(db, v, dry_run=True)
    async with db.session() as s:
        s.add(Position(deployment_id=dep, symbol="ETHUSDT", side="LONG", size=1, entry_price=100, status="OPEN",
                       dry_run=True, opened_at=datetime.now(UTC)))
        await s.commit()
    assert await eng._live_rows_without_keys() == 0                    # a paper row is no concern


async def test_the_pnl_audit_reads_nothing_without_keys(db, tmp_path):
    import asyncio

    from relay.runtime import Runtime
    from tests.test_venue_setup import _cfg, _deployment
    v, _ = _keyless()
    rt = Runtime(_cfg(config_dir=str(_deployment(tmp_path, "venue:\n  exchange: fakex\n  environment: testnet\n")),
                      pnl_reconcile_interval_s=0), db=db, client=v)
    await rt.start()
    calls = []

    async def audit():
        calls.append(1)
    rt.pnl_audit = audit
    try:
        task = asyncio.create_task(rt._pnl_audit_loop())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert calls == []
    finally:
        await rt.stop()


async def test_paper_without_keys_sizes_from_the_owner_s_paper_equity(db):
    from relay.venue.paper import NO_EQUITY, PaperAccount
    v, x = _keyless()
    eng, _ = await _engine(db, PaperAccount(v, None), dry_run=True)
    r = await eng.execute_entry(_sig())
    assert not r["success"] and "paper_equity" in r["reason"]
    assert (await PaperAccount(v, None).get_balance_safe()) == (0.0, NO_EQUITY)
    assert (await PaperAccount(v, 1234.5).get_balance_safe()) == (1234.5, "paper_equity")
    assert not {"fetch_balance"} & set(x.names())


async def test_the_runtime_gives_a_keyless_paper_venue_the_paper_equity(db, tmp_path):
    from relay.runtime import Runtime
    from relay.venue.paper import PaperAccount
    from tests.test_venue_setup import _cfg, _deployment
    v, _ = _keyless()
    rt = Runtime(_cfg(config_dir=str(_deployment(tmp_path, "paper_equity: 2500\nvenue:\n  exchange: fakex\n"
                                                           "  environment: testnet\n")),
                      pnl_reconcile_interval_s=0), db=db, client=v)
    await rt.start()
    try:
        assert isinstance(rt.client, PaperAccount)
        assert (await rt.client.get_balance_safe()) == (2500.0, "paper_equity")
    finally:
        await rt.stop()
