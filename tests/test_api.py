"""REST API: authentication tiers and a full dry-run round trip through the real runtime + engine."""
from pathlib import Path

import httpx
import pytest

from relay.api.app import create_app
from relay.config import Settings
from relay.runtime import Runtime
from tests.test_engine import FakeBybit, _no_sleep  # noqa: F401  (autouse fixture)

KEY, READ = "test-write-key", "test-read-key"


def _cfg(**kw):
    base = {"api_key": KEY, "api_readonly_key": READ, "private_ws_enabled": False, "dry_run": True,
            "testnet": True, "pnl_reconcile_interval_s": 3600,
            # Never the owner's own config/ (deployment and source files in the working directory).
            "config_dir": str(Path(__file__).parent / "no-config-here")}
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
async def api(db):
    """(client, fake exchange, runtime) with the app's startup/shutdown run."""
    fake = FakeBybit()
    rt = Runtime(_cfg(), db=db, client=fake)
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://relay",
                                     headers={"Authorization": f"Bearer {KEY}"}) as client:
            yield client, fake, rt


async def _bare(cfg, db, path="/v1/status", method="GET", headers=None):
    rt = Runtime(cfg, db=db, client=FakeBybit())
    app = create_app(rt)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as c):
        return await c.request(method, path, headers=headers or {})


# ---- authentication ------------------------------------------------------------------

async def test_health_is_public(db):
    r = await _bare(_cfg(), db, "/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_missing_and_wrong_keys_are_refused(db):
    assert (await _bare(_cfg(), db)).status_code == 401
    assert (await _bare(_cfg(), db, headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_both_header_styles_work(db):
    assert (await _bare(_cfg(), db, headers={"Authorization": f"Bearer {KEY}"})).status_code == 200
    assert (await _bare(_cfg(), db, headers={"X-API-Key": KEY})).status_code == 200


async def test_read_only_key_can_read_but_never_trade(db):
    assert (await _bare(_cfg(), db, headers={"X-API-Key": READ})).status_code == 200
    r = await _bare(_cfg(), db, "/v1/close-all", "POST", headers={"X-API-Key": READ})
    assert r.status_code == 403


async def test_no_key_configured_fails_closed_outside_development(db):
    assert (await _bare(_cfg(api_key="", api_readonly_key=""), db)).status_code == 503
    assert (await _bare(_cfg(api_key="", api_readonly_key="", env="development"), db)).status_code == 200


async def test_metrics_need_a_key(db):
    assert (await _bare(_cfg(), db, "/metrics")).status_code == 401


# ---- dry-run round trip -----------------------------------------------------------------

ORDER = {"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95.0}
EXCHANGE_WRITES = {"place_market_order", "set_trading_stop", "set_leverage", "switch_isolated_margin",
                   "cancel_order"}


async def test_status_reports_safe_defaults(api):
    client, _, _ = api
    s = (await client.get("/v1/status")).json()
    assert s["dry_run"] is True and s["testnet"] is True and s["engine_running"] is True


async def test_dry_run_open_reduce_close_round_trip(api):
    client, fake, _ = api
    r = (await client.post("/v1/orders", json=ORDER)).json()
    assert r["status"] == "executed" and r["result"]["dry_run"] is True
    pid = r["result"]["position_id"]

    (pos,) = (await client.get("/v1/positions")).json()
    assert pos["id"] == pid and pos["dry_run"] is True and pos["size"] == 5.0
    assert pos["entry_meta"]["requested_size"] == {"notional_usd": 500}     # an object, not JSON text

    red = (await client.post(f"/v1/positions/{pid}/reduce", json={"close_pct": 40})).json()
    assert red["remaining_qty"] == 3.0

    fake.price = 104.0
    closed = (await client.post(f"/v1/positions/{pid}/close")).json()
    assert closed["status"] == "CLOSED" and closed["pnl_source"] == "dry_run"
    assert closed["pnl"] == pytest.approx(12.0)                  # 3 left x +4
    assert closed["dry_run"] is True and isinstance(closed["entry_meta"], dict)
    assert closed["leverage"] is None                            # none configured: none invented

    sig = (await client.get(f"/v1/signals/{r['id']}")).json()
    assert sig["status"] == "executed" and sig["source"] == "api"
    assert not EXCHANGE_WRITES & set(fake.names())               # nothing reached the exchange


async def test_repeat_client_id_is_never_executed_twice(api):
    client, _, _ = api
    first = (await client.post("/v1/orders", json={**ORDER, "client_id": "abc-1"})).json()
    again = (await client.post("/v1/orders", json={**ORDER, "client_id": "abc-1"})).json()
    assert first["status"] == "executed" and again == {"id": first["id"], "status": "duplicate",
                                                       "signal_status": "executed"}
    assert len((await client.get("/v1/positions")).json()) == 1


async def test_live_positions_on_the_account_do_not_block_paper_orders(db):
    """An account holding more real positions than the user's position limit must not stop a
    simulated order: the risk checks count positions of the engine's own mode."""
    from datetime import UTC, datetime
    rt = Runtime(_cfg(max_positions=5), db=db, client=FakeBybit())
    app = create_app(rt)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay",
                                  headers={"Authorization": f"Bearer {KEY}"}) as client):
        now = datetime.now(UTC)
        async with rt.db.acquire() as c:
            for i in range(12):
                await c.execute(
                    "INSERT INTO positions (symbol, side, size, entry_price, status, opened_at, created_at,"
                    " updated_at) VALUES ($1, 'SHORT', 1, 1, 'OPEN', $2, $2, $2)", f"L{i}USDT", now)
        r = (await client.post("/v1/orders", json=ORDER)).json()
    assert r["status"] == "executed" and r["result"]["dry_run"] is True


async def test_a_refused_order_is_a_422_logged_with_its_reason(api):
    client, _, _ = api
    resp = await client.post("/v1/orders", json={**ORDER, "stop_price": None})
    r = resp.json()
    assert resp.status_code == 422
    assert r["status"] == "skipped" and r["reasons"] == ["stop_required"] and r["result"]["reason"] == "stop_required"
    sig = (await client.get(f"/v1/signals/{r['id']}")).json()
    assert sig["status"] == "skipped" and sig["reasons"] == ["stop_required"]


async def test_invalid_size_is_a_422(api):
    client, _, _ = api
    r = await client.post("/v1/orders", json={**ORDER, "size": {"notional_usd": 5, "qty": 1}})
    assert r.status_code == 422


async def test_close_all_then_emergency_stop(api):
    client, fake, rt = api
    await client.post("/v1/orders", json=ORDER)
    await client.post("/v1/orders", json={**ORDER, "symbol": "ETHUSDT"})
    res = (await client.post("/v1/close-all")).json()
    assert res["success"] and res["positions_closed"] == 2
    assert (await client.get("/v1/positions")).json() == []
    await client.post("/v1/orders", json=ORDER)
    res = (await client.post("/v1/emergency-stop")).json()
    assert res["positions_closed"] == 1 and rt.engine._running is False
    # Halted: no new entry until restart, and it shows.
    resp = await client.post("/v1/orders", json={**ORDER, "client_id": "after-stop"})
    r = resp.json()
    assert resp.status_code == 503 and r["status"] == "skipped" and r["result"]["reason"] == "engine_halted"
    assert (await client.get(f"/v1/signals/{r['id']}")).json()["reasons"] == ["engine_halted"]
    assert (await client.get("/v1/status")).json()["halted"] == "emergency_stop"
    health = await client.get("/health")
    assert health.status_code == 503 and health.json()["status"] == "halted"
    assert (await client.get("/v1/positions")).json() == []
    assert not EXCHANGE_WRITES & set(fake.names())


async def test_unknown_position_is_404(api):
    client, _, _ = api
    assert (await client.post("/v1/positions/999/close")).status_code == 404


async def test_metrics(api):
    client, _, _ = api
    await client.post("/v1/orders", json=ORDER)
    text = (await client.get("/metrics")).text
    assert "relay_dry_run 1" in text and 'relay_open_positions{mode="paper"} 1' in text


async def test_runtime_creates_the_default_deployment(api):
    _, _, rt = api
    from relay.db.models import Deployment
    async with rt.db.session() as s:
        dep = await s.get(Deployment, rt.deployment_id)
    assert dep.name == "default" and dep.dry_run is True


def test_cli_version_and_db_upgrade(tmp_path, monkeypatch, capsys):
    from relay import cli
    from relay.config import settings
    assert cli.main(["version"]) == 0 and "quanti-relay" in capsys.readouterr().out
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    assert cli.main(["db", "upgrade"]) == 0
    assert (tmp_path / "cli.db").exists()


def test_output_floats_are_trimmed_not_altered():
    from relay.api.app import _clean
    pnl = (77721.7 - 77676.5) * 0.001                            # 0.04519999999999709 in binary
    assert pnl != 0.0452 and _clean(pnl) == 0.0452
    assert _clean({"a": [77676.5, 1e-9, float("inf")], "b": True, "c": 3}) == {"a": [77676.5, 1e-9, None], "b": True, "c": 3}


async def test_runtime_clients_follow_the_runtimes_own_settings(db, monkeypatch):
    import relay.exchange.bybit.client_factory as cf
    seen = {}

    def _factory(**kw):
        seen.update(kw)
        return FakeBybit()

    monkeypatch.setattr(cf, "get_bybit_client", _factory)
    rt = Runtime(_cfg(testnet=False, bybit_demo=True), db=db)
    await rt.start()
    await rt.stop()
    assert seen == {"demo": True, "testnet": False}


async def test_a_failed_start_cleans_up(db):
    rt = Runtime(_cfg(), db=db, client=FakeBybit())
    stopped = []
    original_stop = rt.stop

    async def _stop():
        stopped.append(True)
        await original_stop()

    async def _broken_start():
        raise RuntimeError("engine failed to start")

    rt.stop, rt.start = _stop, _broken_start
    app = create_app(rt)
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert stopped == [True]



# ---- request validation and status codes --------------------------------------------------

@pytest.mark.parametrize("payload", [
    {**ORDER, "take_profit": 110},                        # misspelt field: refused, not ignored
    {**ORDER, "size": {"qty": "NaN"}},
    {**ORDER, "size": {"qty": -1}},
    {**ORDER, "stop_price": "Infinity"},
    {**ORDER, "symbol": ""},
    {**ORDER, "symbol": "BTC/USDT"},
])
async def test_malformed_orders_are_refused_before_anything_happens(api, payload):
    client, fake, _ = api
    r = await client.post("/v1/orders", content=__import__("json").dumps(payload),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and fake.calls == []
    assert "detail" in r.json()                           # refused by request validation, not later by the engine


async def test_symbols_are_upper_cased_everywhere(api):
    client, _, _ = api
    r = (await client.post("/v1/orders", json={**ORDER, "symbol": "btcusdt"})).json()
    sig = (await client.get(f"/v1/signals/{r['id']}")).json()
    assert sig["symbol"] == "BTCUSDT" and r["result"]["success"]


async def test_a_venue_failure_is_a_502(api):
    client, fake, _ = api

    async def _no_price(symbol, category="linear"):
        raise RuntimeError("venue down")

    fake.get_ticker = _no_price
    r = await client.post("/v1/orders", json=ORDER)
    assert r.status_code == 502


async def test_positions_can_be_filtered_and_paged_and_include_closing_rows(api):
    client, _, rt = api
    await client.post("/v1/orders", json=ORDER)
    await client.post("/v1/orders", json={**ORDER, "symbol": "ETHUSDT"})
    async with rt.db.acquire() as c:
        await c.execute("UPDATE positions SET status = 'CLOSING' WHERE symbol = 'ETHUSDT'")
    assert len((await client.get("/v1/positions")).json()) == 2             # CLOSING still counts as open
    assert len((await client.get("/v1/positions?mode=live")).json()) == 0
    assert len((await client.get("/v1/positions?mode=paper&limit=1&offset=1")).json()) == 1
    assert (await client.get("/v1/positions?status=closed")).json() == []


async def test_the_docs_page_is_public_but_every_call_needs_the_key(db):
    assert (await _bare(_cfg(), db, "/docs")).status_code == 200
    spec = (await _bare(_cfg(), db, "/openapi.json")).json()
    assert spec["components"]["securitySchemes"]["apiKey"]["scheme"] == "bearer"
    assert (await _bare(_cfg(), db, "/v1/positions")).status_code == 401
    assert (await _bare(_cfg(), db, "/v1/status", "OPTIONS")).status_code == 401   # no unauthenticated OPTIONS


def test_auth_and_trading_cannot_use_different_settings():
    rt = Runtime(_cfg())
    with pytest.raises(ValueError):
        create_app(rt, _cfg(api_key="other"))


def test_cli_refuses_a_network_address_without_a_key(monkeypatch):
    import uvicorn

    from relay import cli
    from relay import config as config_mod
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append(k))   # never actually serve in a test
    monkeypatch.setattr(config_mod.settings, "api_key", "")
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2 and served == []
    monkeypatch.setattr(config_mod.settings, "api_key", "k" * 40)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 0 and served


def test_cli_without_a_command_prints_help(capsys):
    from relay import cli
    assert cli.main([]) == 0 and "usage" in capsys.readouterr().out


async def test_a_close_the_venue_refuses_is_a_502_and_an_internal_error_a_500(api, monkeypatch):
    client, fake, rt = api
    pid = (await client.post("/v1/orders", json=ORDER)).json()["result"]["position_id"]

    async def _refused(**kw):
        return {"success": False, "error": "Bybit API error 110017: position is zero"}

    monkeypatch.setattr(rt.engine.order_executor, "close_position", _refused)
    r = await client.post(f"/v1/positions/{pid}/close")
    assert r.status_code == 502

    async def _bug(*a, **k):
        raise KeyError("unexpected")

    monkeypatch.setattr(rt.engine, "execute_exit", _bug)
    r = await client.post(f"/v1/positions/{pid}/close")
    assert r.status_code == 500 and "unexpected" not in r.text



async def test_close_all_that_leaves_positions_open_is_a_502(api, monkeypatch):
    client, fake, rt = api
    await client.post("/v1/orders", json=ORDER)

    async def _refused(**kw):
        return {"success": False, "error": "rate limited"}

    monkeypatch.setattr(rt.engine.order_executor, "close_position_fast", _refused)
    r = await client.post("/v1/close-all")
    assert r.status_code == 502 and r.json()["errors"]


async def test_a_signal_that_cannot_be_recorded_is_not_mistaken_for_a_duplicate(db, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from relay.signals.log import SignalLog
    log = SignalLog(db)
    real_session = db.session

    class _Failing:
        def __init__(self):
            self.s = real_session()

        async def __aenter__(self):
            s = await self.s.__aenter__()

            async def _commit():
                raise IntegrityError("INSERT", {}, Exception("NOT NULL constraint failed"))
            s.commit = _commit
            return s

        async def __aexit__(self, *a):
            return await self.s.__aexit__(*a)

    monkeypatch.setattr(db, "session", _Failing)
    with pytest.raises(RuntimeError):
        await log.record("api", "x1", {})



async def test_a_risk_check_that_cannot_reach_the_venue_is_a_502_not_a_refusal(api):
    client, fake, _ = api

    async def _down(coin="USDT"):
        return 0.0, "unavailable"

    fake.get_balance_safe = _down
    r = await client.post("/v1/orders", json=ORDER)
    assert r.status_code == 502 and "Unable to retrieve" in r.json()["result"]["reason"]


async def test_an_unknown_daily_loss_is_a_503(db):
    from datetime import UTC, datetime
    rt = Runtime(_cfg(daily_loss_limit_pct=5), db=db, client=FakeBybit())
    app = create_app(rt)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay",
                                  headers={"Authorization": f"Bearer {KEY}"}) as client):
        now = datetime.now(UTC)
        async with rt.db.acquire() as c:
            await c.execute("INSERT INTO positions (deployment_id, symbol, side, size, entry_price, status, opened_at,"
                            " closed_at, pnl, pnl_source, dry_run, created_at, updated_at) VALUES"
                            " ($1, 'X', 'LONG', 1, 1, 'CLOSED', $2, $2, NULL, 'pending', $3, $2, $2)",
                            rt.deployment_id, now, True)
        r = await client.post("/v1/orders", json=ORDER)
    assert r.status_code == 503


async def test_non_ascii_symbols_are_refused(api):
    client, fake, _ = api
    r = await client.post("/v1/orders", json={**ORDER, "symbol": "ＢＴＣＵＳＤＴ"})
    assert r.status_code == 422 and fake.calls == []


async def test_the_read_only_key_cannot_use_other_methods(db):
    r = await _bare(_cfg(), db, "/v1/status", "OPTIONS", headers={"X-API-Key": READ})
    assert r.status_code == 403


async def test_shutdown_closes_everything_even_if_a_step_fails(db, monkeypatch):
    import relay.exchange.bybit.client_factory as cf
    closed = []

    class _Closable(FakeBybit):
        async def close(self):
            closed.append("client")

    monkeypatch.setattr(cf, "get_bybit_client", lambda **kw: _Closable())
    rt = Runtime(_cfg(), db=db)
    await rt.start()

    async def _broken_stop():
        raise RuntimeError("engine stop failed")

    rt.engine.stop = _broken_stop
    await rt.stop()
    assert closed == ["client"]


async def test_closing_a_position_another_request_is_closing_is_a_409(api):
    client, _, rt = api
    pid = (await client.post("/v1/orders", json=ORDER)).json()["result"]["position_id"]
    rt.engine._own_close(pid)
    r = await client.post(f"/v1/positions/{pid}/close")
    assert r.status_code == 409
