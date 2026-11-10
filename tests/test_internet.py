"""Reachable from the internet (R7.1): the client address behind a proxy, the wrong-key lockout, the
address allowlist, the body limit, health that notices a stuck relay, the heartbeat, and the
database copy before an upgrade."""
import asyncio
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from starlette.datastructures import Headers

from relay.api.app import create_app
from relay.api.guard import TRACKED_MAX, Guard, GuardConfigError
from relay.db import backup as backup_mod
from relay.runtime import Runtime
from tests.test_api import KEY, READ, _cfg
from tests.test_engine import FakeBybit, _no_sleep, _real_sleep  # noqa: F401  (autouse fixture)


def _settings(**kw):
    base = {"trusted_proxies": "", "client_ip_header": "X-Forwarded-For", "api_allow_ips": "",
            "auth_max_failures": 3, "auth_failure_window_s": 60, "auth_lockout_s": 120, "max_body_bytes": 0}
    base.update(kw)
    return SimpleNamespace(**base)


# ---- the client's address ------------------------------------------------------------------

def test_without_proxies_the_client_is_the_peer_and_its_header_is_ignored():
    g = Guard(_settings())
    assert g.client("203.0.113.7", Headers({"X-Forwarded-For": "198.51.100.1"})) == "203.0.113.7"


def test_behind_a_trusted_proxy_the_rightmost_outside_entry_is_the_client():
    g = Guard(_settings(trusted_proxies="10.0.0.0/8"))
    # the client wrote a fake address on the left; the proxy appended the real one on the right
    h = Headers({"X-Forwarded-For": "198.51.100.1, 203.0.113.7, 10.0.0.3"})
    assert g.client("10.0.0.2", h) == "203.0.113.7"
    # a peer that is not the proxy cannot speak for anyone else
    assert g.client("192.0.2.9", h) == "192.0.2.9"
    # through the proxy with nothing it added, or only proxies: the request began at the proxy
    assert g.client("10.0.0.2", Headers({})) == "10.0.0.2"
    assert g.client("10.0.0.2", Headers({"X-Forwarded-For": "10.0.0.5"})) == "10.0.0.2"
    assert g.client("10.0.0.2", Headers({"X-Forwarded-For": "198.51.100.9, junk"})) is None


def test_bad_settings_stop_the_start():
    with pytest.raises(GuardConfigError, match="TRUSTED_PROXIES"):
        Guard(_settings(trusted_proxies="10.0.0.0/8, nonsense"))
    with pytest.raises(GuardConfigError, match="API_ALLOW_IPS"):
        Guard(_settings(api_allow_ips="not-a-network"))
    with pytest.raises(GuardConfigError, match="CLIENT_IP_HEADER"):
        Guard(_settings(trusted_proxies="10.0.0.1", client_ip_header=" "))
    with pytest.raises(GuardConfigError, match="AUTH_LOCKOUT_S"):
        Guard(_settings(auth_lockout_s=-1))


def test_ipv6_counts_per_64_and_mapped_ipv4_as_ipv4():
    assert Guard.bucket("2001:db8:1:2::5") == Guard.bucket("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert Guard.bucket("2001:db8:1:3::5") != Guard.bucket("2001:db8:1:2::5")
    assert Guard.bucket("::ffff:203.0.113.7") == "203.0.113.7"
    assert Guard.bucket(None) == Guard.bucket("garbage") == "unknown"


# ---- the lockout ----------------------------------------------------------------------------

def test_the_nth_wrong_key_within_the_window_locks_the_address_for_the_lockout():
    g = Guard(_settings())
    assert not g.failed("203.0.113.7", now=0) and not g.failed("203.0.113.7", now=10)
    assert g.locked_for("203.0.113.7", now=10) == 0
    assert g.failed("203.0.113.7", now=20)
    assert g.locked_for("203.0.113.7", now=20) == 120
    assert g.locked_for("203.0.113.8", now=20) == 0          # another address is not affected
    assert g.locked_for("203.0.113.7", now=139) == 1
    assert g.locked_for("203.0.113.7", now=140) == 0
    assert g.locked_for("203.0.113.7", now=500) == 0          # long after, too: never a negative wait


def test_old_failures_leave_the_window_and_a_right_key_clears_them():
    g = Guard(_settings())
    g.failed("a", now=0)
    g.failed("a", now=1)
    assert not g.failed("203.0.113.7", now=0)
    assert not g.failed("203.0.113.7", now=30)
    assert not g.failed("203.0.113.7", now=61)              # the first one is out of the window
    g.succeeded("203.0.113.7")
    assert not g.failed("203.0.113.7", now=62) and not g.failed("203.0.113.7", now=63)
    assert g.failed("203.0.113.7", now=64)


def test_zero_failures_means_never_locked_out():
    g = Guard(_settings(auth_max_failures=0))
    assert not any(g.failed("203.0.113.7", now=i) for i in range(50))


def test_many_addresses_cannot_fill_the_memory():
    g = Guard(_settings())
    for i in range(TRACKED_MAX + 50):
        g.failed(f"10.{i >> 16 & 255}.{i >> 8 & 255}.{i & 255}", now=0)
    assert len(g._failures) == TRACKED_MAX
    assert "10.0.0.0" not in g._failures                     # the oldest went first


# ---- through the API ------------------------------------------------------------------------

async def _client(app, address="203.0.113.7", **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(address, 4000)),
                             base_url="http://relay", **kw)


@pytest.fixture
async def relay(db):
    rt = Runtime(_cfg(auth_max_failures=3, auth_failure_window_s=60, auth_lockout_s=120,
                      api_allow_ips="", max_body_bytes=4096), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        yield app, rt


async def test_an_address_with_too_many_wrong_keys_is_refused_even_with_the_right_one(relay):
    app, rt = relay
    async with await _client(app) as c, await _client(app, "198.51.100.4") as other:
        for _ in range(2):
            assert (await c.get("/v1/status", headers={"X-API-Key": "guess"})).status_code == 401
        assert (await c.get("/v1/status", headers={"X-API-Key": KEY})).status_code == 200
        for _ in range(3):                                   # the right key cleared the count
            assert (await c.get("/v1/status", headers={"X-API-Key": "guess"})).status_code == 401
        r = await c.get("/v1/status", headers={"X-API-Key": KEY})
        assert r.status_code == 429 and 0 < int(r.headers["Retry-After"]) <= 121
        # the safety routes still take the full key: a neighbour on a shared address cannot keep
        # the owner from stopping; any other key there is refused unread
        assert (await c.post("/v1/close-all", headers={"X-API-Key": "guess"})).status_code == 429
        assert (await c.post("/v1/close-all", headers={"X-API-Key": READ})).status_code == 429
        assert (await c.post("/v1/close-all", headers={"X-API-Key": KEY})).status_code == 200
        assert (await c.get("/v1/positions", headers={"X-API-Key": KEY})).status_code == 429
        assert (await other.get("/v1/status", headers={"X-API-Key": KEY})).status_code == 200
        assert (await c.get("/health")).status_code == 200    # monitors still see it
    events = await rt.events.recent(kind="auth.locked")
    assert len(events) == 1 and events[0].data == {"address": "203.0.113.7"}
    assert "guess" not in events[0].message


async def test_the_read_key_counts_as_right_and_wrong_keys_on_write_are_403_not_failures(relay):
    app, _ = relay
    async with await _client(app) as c:
        for _ in range(5):
            assert (await c.post("/v1/emergency-stop", headers={"X-API-Key": READ})).status_code == 403
        assert (await c.get("/v1/status", headers={"X-API-Key": READ})).status_code == 200


async def test_a_lesser_key_never_clears_the_count_of_wrong_full_keys(relay):
    app, _ = relay
    async with await _client(app) as c:
        for _ in range(2):
            assert (await c.get("/v1/status", headers={"X-API-Key": "guess"})).status_code == 401
            assert (await c.get("/v1/status", headers={"X-API-Key": READ})).status_code == 200
        assert (await c.get("/v1/status", headers={"X-API-Key": "guess"})).status_code == 401
        assert (await c.get("/v1/status", headers={"X-API-Key": READ})).status_code == 429


def test_every_line_of_the_forwarded_header_is_read():
    g = Guard(_settings(trusted_proxies="10.0.0.0/8"))
    # a proxy that adds its own line: the client's line comes first, the proxy's last
    h = Headers(raw=[(b"x-forwarded-for", b"198.51.100.1"), (b"x-forwarded-for", b"203.0.113.7")])
    assert g.client("10.0.0.2", h) == "203.0.113.7"


def test_the_locked_addresses_are_capped_too():
    g = Guard(_settings(auth_max_failures=1))
    for i in range(TRACKED_MAX + 5):
        g.failed(f"2001:db8:{i // 65536:x}:{i % 65536:x}::1", now=0)
    assert len(g._locked) == TRACKED_MAX
    assert g.locked_for(f"2001:db8:0:{TRACKED_MAX + 4:x}::1", now=1) > 0       # the newest stays locked


async def test_behind_the_proxy_each_client_is_locked_out_on_its_own(db):
    rt = Runtime(_cfg(auth_max_failures=2, trusted_proxies="10.0.0.0/8"), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app), await _client(app, "10.0.0.2") as proxy:
        for _ in range(2):
            await proxy.get("/v1/status", headers={"X-API-Key": "x", "X-Forwarded-For": "203.0.113.7"})
        locked = await proxy.get("/v1/status", headers={"X-API-Key": KEY, "X-Forwarded-For": "203.0.113.7"})
        fine = await proxy.get("/v1/status", headers={"X-API-Key": KEY, "X-Forwarded-For": "198.51.100.4"})
    assert locked.status_code == 429 and fine.status_code == 200


async def test_the_allowlist_closes_the_api_console_and_docs_but_not_health_or_signals(db):
    rt = Runtime(_cfg(api_allow_ips="192.0.2.0/24"), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with await _client(app, "203.0.113.7") as out:
            for path in ("/v1/status", "/console", "/docs", "/openapi.json"):
                assert (await out.get(path, headers={"X-API-Key": KEY})).status_code == 403, path
            assert (await out.get("/health")).status_code == 200
            # signal intake answers by the source's rules (none here), not the allowlist
            r = await out.post("/v1/sources/nobody/signals", json={})
            assert r.status_code != 403 or "ALLOW_IPS" not in r.text
            r = await out.post("/v1/signals", json={})
            assert "ALLOW_IPS" not in r.text
        async with await _client(app, "192.0.2.10") as inside:
            assert (await inside.get("/v1/status", headers={"X-API-Key": KEY})).status_code == 200
            assert (await inside.get("/console")).status_code == 200


async def test_a_body_over_the_limit_is_refused_by_length_or_while_it_streams(relay):
    app, _ = relay
    async with await _client(app, headers={"X-API-Key": KEY}) as c:
        r = await c.put("/v1/setup/source/big", content=b"x" * 5000, headers={"Content-Type": "application/json"})
        assert r.status_code == 413

        async def chunks():
            for _ in range(10):
                yield b"x" * 1000
        r = await c.put("/v1/setup/source/big", content=chunks(), headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        r = await c.put("/v1/setup/source/small", content=b"{}", headers={"Content-Type": "application/json"})
        assert r.status_code != 413
        # refused by its stated length before anything reads it, even where nothing would
        r = await c.request("GET", "/v1/status", content=b"x" * 5000)
        assert r.status_code == 413


async def test_a_body_that_trickles_in_is_cut_off(monkeypatch):
    from relay.api import guard as guard_mod
    monkeypatch.setattr(guard_mod, "BODY_TIMEOUT_S", 0.2)
    seen = []

    async def endless_app(scope, receive, send):
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        seen.append(body)
        await receive()                         # listening for a disconnect: no deadline there
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run(messages, delay):
        sent = []
        queue = list(messages)

        async def receive():
            if not queue:                       # the body is in: the client goes away later than the deadline
                await _real_sleep(0.4)
                return {"type": "http.disconnect"}
            await _real_sleep(delay)
            return queue.pop(0)

        async def send(m):
            sent.append(m)
        await guard_mod.BodyLimitMiddleware(endless_app, 10_000)({"type": "http", "headers": []}, receive, send)
        return sent[0]["status"]

    slow = [{"type": "http.request", "body": b"x", "more_body": True}] * 5
    assert await run(slow, 0.1) == 408 and not seen
    assert await run([{"type": "http.request", "body": b"x", "more_body": False}], 0.01) == 200
    assert seen == [b"x"]                       # whole, and the wait for a disconnect after it was not cut


# ---- health, stuck loops and the heartbeat ---------------------------------------------------

async def test_health_says_stuck_when_a_loop_has_not_finished_a_round_for_too_long(relay):
    app, rt = relay
    async with await _client(app) as c:
        assert (await c.get("/health")).json()["status"] == "ok"
        at, interval = rt.engine._beats["stop_rules"]
        rt.engine._beats["stop_rules"] = (at - 2 * interval - 601, interval)
        r = await c.get("/health")
        assert r.status_code == 503 and r.json() == {"service": "quanti-relay", "status": "stuck"}
        assert (await c.get("/v1/status", headers={"X-API-Key": KEY})).json()["health"] == {"status": "stuck", "loops": ["stop_rules"]}
        rt.engine._beats["stop_rules"] = (at - 2 * interval - 599, interval)
        assert (await c.get("/health")).status_code == 200


async def test_a_loop_that_ended_is_stuck_and_reported_once(relay):
    _, rt = relay
    task = rt.engine._tasks[rt.engine._loop_names.index("position_sync")]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert rt.engine.stuck_loops() == ["position_sync"]
    await rt._report_stuck()
    await rt._report_stuck()
    events = await rt.events.recent(kind="relay.stuck")
    assert len(events) == 1 and events[0].severity == "critical"


async def test_a_loop_that_fails_every_round_is_stuck_and_one_good_round_clears_it(relay, monkeypatch):
    _, rt = relay
    e = rt.engine
    at, _ = e._beats["stop_audit"]
    e._beats["stop_audit"] = (at - 10_000, 300.0)              # its last good round was long ago
    sleeps = []

    async def no_wait(s):
        sleeps.append(s)
        e._running = False                                      # one round, then the loop ends

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    async def failing():
        raise RuntimeError("the exchange refuses the key")
    monkeypatch.setattr(e, "audit_stops", failing)
    await e._stop_audit_loop()
    e._running = True
    assert sleeps and "stop_audit" in e.stuck_loops()          # the failed round was no sign of life

    async def fine():
        return {}
    monkeypatch.setattr(e, "audit_stops", fine)
    await e._stop_audit_loop()
    e._running = True
    assert "stop_audit" not in e.stuck_loops()


async def test_the_heartbeat_calls_while_healthy_and_stops_when_stuck(db, monkeypatch):
    calls = []
    real = httpx.AsyncClient

    def fake_client(**kw):
        return real(transport=httpx.MockTransport(lambda req: calls.append(str(req.url)) or httpx.Response(200)),
                    **kw)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    rt = Runtime(_cfg(heartbeat_url="https://monitor.example/ping/abc", heartbeat_interval_s=1),
                 db=db, client=FakeBybit())
    await rt.start()
    try:
        for _ in range(100):
            if calls:
                break
            await _real_sleep(0.01)
        assert calls and calls[0] == "https://monitor.example/ping/abc"
        while rt.engine._beats["stop_rules"][1] == 0:       # its first round, which would beat again
            await _real_sleep(0.01)
        rt.engine._beats["stop_rules"] = (0.0, 10.0)        # stuck
        await _real_sleep(0.05)
        n = len(calls)
        await _real_sleep(0.05)
        assert len(calls) == n
    finally:
        await rt.stop()


async def test_no_heartbeat_url_means_no_calls(db):
    rt = Runtime(_cfg(), db=db, client=FakeBybit())
    await rt.start()
    try:
        assert not any("_heartbeat_loop" in repr(t.get_coro()) for t in rt._tasks)
    finally:
        await rt.stop()


# ---- the database copy before an upgrade --------------------------------------------------

def _downgrade_one(url):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "relay" / "db" / "migrations"))
    cfg.attributes["url"] = url
    command.downgrade(cfg, "-1")


def test_an_upgrade_copies_the_database_first(tmp_path):
    from relay.runtime import upgrade_schema
    db_file = tmp_path / "relay.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    upgrade_schema(url)
    assert not (tmp_path / "backups").exists()                # a new database: nothing to keep
    upgrade_schema(url)
    assert not (tmp_path / "backups").exists()                # already at the latest: no copy
    _downgrade_one(url)
    prev = backup_mod.current_revision(db_file)
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE marker (x)")
    con.execute("INSERT INTO marker VALUES (42)")
    con.commit()
    con.close()
    upgrade_schema(url)
    copies = list((tmp_path / "backups").iterdir())
    assert len(copies) == 1 and copies[0].name.startswith(f"relay-{prev}-")
    con = sqlite3.connect(copies[0])
    assert con.execute("SELECT x FROM marker").fetchone() == (42,)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (prev,)
    con.close()
    assert backup_mod.current_revision(db_file) != prev       # and the upgrade itself went on


def test_only_the_newest_copies_are_kept(tmp_path):
    db_file = tmp_path / "relay.db"
    sqlite3.connect(db_file).close()
    made = [backup_mod.backup(db_file, "r1", now=datetime(2026, 1, d, tzinfo=UTC))
            for d in range(1, backup_mod.BACKUPS_KEEP + 3)]
    (tmp_path / "backups" / "notes.txt").write_text("mine")
    backup_mod.backup(db_file, "r1", now=datetime(2026, 2, 1, tzinfo=UTC))
    left = sorted(p.name for p in (tmp_path / "backups").iterdir())
    # the newest five, the oldest copy of the revision (the one before an upgrade), and the notes
    assert "notes.txt" in left and len(left) == backup_mod.BACKUPS_KEEP + 2
    assert made[0].name in left and made[1].name not in left and made[2].name not in left
    assert made[3].name in left
    newer = backup_mod.backup(db_file, "r2", now=datetime(2026, 3, 1, tzinfo=UTC))
    for d in range(2, 9):
        backup_mod.backup(db_file, "r2", now=datetime(2026, 3, d, tzinfo=UTC))
    left = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert made[0].name in left and newer.name in left and len(left) == backup_mod.BACKUPS_KEEP + 3


def test_sqlite_path_is_none_for_memory_and_postgres():
    assert backup_mod.sqlite_path("sqlite+aiosqlite:///:memory:") is None
    assert backup_mod.sqlite_path("postgresql+asyncpg://u:p@h/db") is None
    assert str(backup_mod.sqlite_path("sqlite+aiosqlite:////data/relay.db")) == "/data/relay.db"


def test_relay_db_backup(tmp_path, monkeypatch, capsys):
    from relay import cli
    from relay import config as config_mod
    db_file = tmp_path / "relay.db"
    monkeypatch.setattr(config_mod.settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    assert cli.main(["db", "backup"]) == 2                    # nothing there yet
    sqlite3.connect(db_file).close()
    assert cli.main(["db", "backup"]) == 0 and "backups" in capsys.readouterr().out
    monkeypatch.setattr(config_mod.settings, "database_url", "postgresql+asyncpg://u:p@h/db")
    assert cli.main(["db", "backup"]) == 2 and "pg_dump" in capsys.readouterr().err


def test_a_short_key_on_a_reachable_address_is_refused(monkeypatch):
    import uvicorn

    from relay import cli
    from relay import config as config_mod
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append(k))
    monkeypatch.setattr(config_mod.settings, "api_key", "k" * 31)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2 and not served
    monkeypatch.setattr(config_mod.settings, "api_key", "k" * 32)
    monkeypatch.setattr(config_mod.settings, "api_readonly_key", "r" * 8)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2 and not served
    monkeypatch.setattr(config_mod.settings, "api_readonly_key", "")
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 0 and served[0]["limit_concurrency"] == 100
    monkeypatch.setattr(config_mod.settings, "api_key", "k")
    assert cli.main(["serve"]) == 0                           # localhost: any key
