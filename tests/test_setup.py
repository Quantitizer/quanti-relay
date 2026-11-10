"""Setting the relay up from the console (relay/setup.py and /v1/setup): the files it writes, what it
refuses, and the gates before live."""
import httpx
import pytest
import yaml

from relay import setup
from relay.api.app import create_app
from relay.runtime import Runtime
from tests.test_api import KEY, READ, _cfg
from tests.test_engine import FakeBybit, _no_sleep, _real_sleep  # noqa: F401  (autouse fixture)

LIMITS = {"rails": {"max_notional_usd": 50, "max_gross_leverage": 2}, "risk": {"max_positions": 2,
                                                                          "daily_loss_limit_pct": 3}}


def _read(path):
    return yaml.safe_load(path.read_text())


# ---- the files ----------------------------------------------------------------------------------

def test_a_deployment_is_checked_written_and_its_old_version_kept(tmp_path):
    path = setup.write_config(tmp_path, "deployment", "main", {"venue": {"exchange": "okx", "environment": "testnet"}})
    assert path == tmp_path / "deployments" / "main.yaml"
    assert path.read_text().startswith("# Written by the relay console")
    assert _read(path) == {"venue": {"exchange": "okx", "environment": "testnet"}, "name": "main", "dry_run": True}
    setup.write_config(tmp_path, "deployment", "main", {**_read(path), **LIMITS})
    (old,) = (tmp_path / ".history").iterdir()
    assert "max_positions" not in old.read_text() and _read(path)["risk"]["max_positions"] == 2
    assert oct(path.stat().st_mode & 0o777) == oct(0o644) or path.stat().st_mode & 0o044


def test_a_bad_deployment_is_refused_and_nothing_is_written(tmp_path):
    with pytest.raises(setup.SetupError, match="max_positions"):
        setup.write_config(tmp_path, "deployment", "main", {"risk": {"max_positions": 1.5}})
    with pytest.raises(setup.SetupError, match="environment"):
        setup.write_config(tmp_path, "deployment", "main", {"venue": {"exchange": "okx"}})
    assert not (tmp_path / "deployments").exists() or not list((tmp_path / "deployments").iterdir())


def test_the_console_never_makes_a_deployment_live_through_a_plain_save(tmp_path):
    path = setup.write_config(tmp_path, "deployment", "main", {"dry_run": False, **LIMITS})
    assert _read(path)["dry_run"] is True
    setup.set_dry_run(tmp_path, False)
    path = setup.write_config(tmp_path, "deployment", "main", {**LIMITS, "risk": {**LIMITS["risk"], "max_positions": 3}})
    assert _read(path)["dry_run"] is False                    # a live file stays live while the gates still hold


def test_a_paper_file_with_every_limit_stays_paper_through_any_save(tmp_path):
    setup.write_config(tmp_path, "deployment", "main", LIMITS)
    path = setup.write_config(tmp_path, "deployment", "main", {**LIMITS, "dry_run": False})
    assert _read(path)["dry_run"] is True


@pytest.mark.parametrize("change", [
    {"venue": {"exchange": "okx", "environment": "testnet"}},                         # another exchange
    {"rails": {"max_gross_leverage": 2}},                                             # a core limit emptied
    {"risk": {"max_positions": 2}},
])
def test_a_live_file_goes_back_to_paper_when_a_save_touches_what_going_live_checked(tmp_path, change):
    setup.write_config(tmp_path, "deployment", "main", LIMITS)
    setup.set_dry_run(tmp_path, False)
    path = setup.write_config(tmp_path, "deployment", "main", {**LIMITS, **change})
    assert _read(path)["dry_run"] is True


def test_a_source_s_name_is_its_file_s(tmp_path):
    data = {"auth": "hmac", "deployments": ["main"]}
    setup.write_config(tmp_path, "source", "a", {**data, "name": "b", "secret_env": "RELAY_SOURCE_A_SECRET"},
                       environ={})
    assert _read(tmp_path / "sources" / "a.yaml")["name"] == "a"
    (tmp_path / "sources" / "hand.yaml").write_text("name: c\nauth: hmac\nsecret_env: X\ndeployments: [main]\n")
    with pytest.raises(setup.SetupError, match="already named 'c'"):
        setup.write_config(tmp_path, "source", "c", {**data, "secret_env": "RELAY_SOURCE_C_SECRET"}, environ={})


def test_the_deployment_s_name_is_not_changed_here(tmp_path):
    setup.write_config(tmp_path, "deployment", "main", {})
    with pytest.raises(setup.SetupError, match="not changed here"):
        setup.write_config(tmp_path, "deployment", "other", {})


@pytest.mark.parametrize("name", ["../evil", "Main", "", "a" * 51, "x/y"])
def test_a_name_that_is_not_a_plain_name_is_refused(tmp_path, name):
    with pytest.raises(setup.SetupError, match="name"):
        setup.write_config(tmp_path, "source", name, {})


def test_a_source_is_written_before_its_secret_is_set(tmp_path):
    data = {"auth": "hmac", "secret_env": "RELAY_SOURCE_BOT_SECRET", "deployments": ["main"], "actions": ["open"]}
    path = setup.write_config(tmp_path, "source", "bot", data, environ={})
    assert _read(path)["actions"] == ["open"]
    with pytest.raises(setup.SetupError, match="actions"):
        setup.write_config(tmp_path, "source", "bot", {**data, "actions": ["withdraw"]}, environ={})


@pytest.mark.parametrize("kind, data", [
    ("sink", {"type": "telegram", "bot_token_env": "RELAY_OKX_API_SECRET", "chat_id_env": "X"}),
    ("sink", {"type": "webhook", "url_env": "BYBIT_API_KEY"}),
    ("sink", {"type": "discord", "webhook_url_env": "RELAY_API_KEY"}),
    ("source", {"auth": "hmac", "secret_env": "RELAY_HYPERLIQUID_PRIVATE_KEY", "deployments": ["main"]}),
    ("source", {"auth": "hmac", "secret_env": "POSTGRES_PASSWORD", "deployments": ["main"]}),
])
def test_a_file_may_not_name_the_relay_s_own_keys(tmp_path, kind, data):
    with pytest.raises(setup.SetupError, match="variables of its own only"):
        setup.write_config(tmp_path, kind, "x", data, environ={})


@pytest.mark.parametrize("variable", ["RELAY_SOURCE_A_SECRET", "RELAY_EVENTS_WEBHOOK_SECRET", "PGPASSWORD",
                                      "AWS_SECRET_ACCESS_KEY", "RELAY_SINK_X_"])
def test_a_sink_may_not_name_any_variable_but_its_own(tmp_path, variable):
    setup.write_config(tmp_path, "source", "a", {"auth": "hmac", "secret_env": "RELAY_SOURCE_A_SECRET",
                                                 "deployments": ["main"]}, environ={})
    with pytest.raises(setup.SetupError, match="of its own only"):
        setup.write_config(tmp_path, "sink", "x", {"type": "telegram", "bot_token_env": variable,
                                                   "chat_id_env": "RELAY_SINK_X_CHAT"}, environ={})


def test_two_files_never_share_a_variable(tmp_path):
    (tmp_path / "sinks").mkdir()
    (tmp_path / "sinks" / "hand.yaml").write_text("type: webhook\nurl_env: RELAY_SINK_X_URL\n")
    with pytest.raises(setup.SetupError, match="already uses"):
        setup.write_config(tmp_path, "sink", "x", {"type": "webhook", "url_env": "RELAY_SINK_X_URL"}, environ={})


def test_one_name_s_history_is_not_pruned_by_another_s(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "HISTORY_KEEP", 1)
    for name in ("a-b", "a"):                     # pruning "a" must leave "a-b"'s versions
        for i in range(3):
            setup.write_config(tmp_path, "sink", name, {"type": "webhook", "min_severity": ["info", "warning",
                               "critical"][i], "url_env": setup.own_prefix("sink", name) + "URL"}, environ={})
    names = sorted(p.name.split("-2026")[0].split("-2")[0] for p in (tmp_path / ".history").iterdir())
    assert len(names) == 2 and any("sinks-a-b" in p.name for p in (tmp_path / ".history").iterdir())


def test_a_sink_is_written_before_its_values_are_set_and_removed_with_a_copy_kept(tmp_path):
    setup.write_config(tmp_path, "sink", "phone", {"type": "telegram", "bot_token_env": "RELAY_SINK_PHONE_TOKEN",
                                                  "chat_id_env": "RELAY_SINK_PHONE_CHAT"}, environ={})
    setup.remove_config(tmp_path, "sink", "phone")
    assert not (tmp_path / "sinks" / "phone.yaml").exists()
    assert len(list((tmp_path / ".history").iterdir())) == 1
    with pytest.raises(setup.SetupError):
        setup.remove_config(tmp_path, "deployment", "main")


def test_a_folder_the_relay_cannot_write_is_said_not_failed_halfway(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    folder.chmod(0o555)
    try:
        assert not setup.writable(folder)
        with pytest.raises(setup.SetupError, match="cannot be written"):
            setup.write_config(folder, "deployment", "main", {})
    finally:
        folder.chmod(0o755)


def test_the_history_keeps_the_last_versions_only(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "HISTORY_KEEP", 3)
    for i in range(6):
        setup.write_config(tmp_path, "deployment", "main", {"risk": {"max_positions": i + 1}})
    assert len(list((tmp_path / ".history").iterdir())) == 3


# ---- the gates before live -----------------------------------------------------------------------

def _dep(**kw):
    from relay.deployment_config import DeploymentFile
    return DeploymentFile.model_validate({"name": "main", **kw})


def test_every_core_limit_is_asked_for():
    assert len(setup.missing_limits(None)) == 4
    assert setup.missing_limits(_dep(**LIMITS)) == []
    assert setup.missing_limits(_dep(rails={"max_notional_pct": 5, "max_gross_leverage": 2},
                                     risk={"max_positions": 1, "daily_loss_limit_pct": 1})) == []
    assert setup.missing_limits(_dep(rails={"max_gross_leverage": 2},
                                     risk={"max_positions": 1, "daily_loss_limit_pct": 1})) == [
        "the largest position (USDT or % of equity)"]


@pytest.mark.parametrize("change, closed", [
    ({}, None), ({"keys_set": False}, "keys"), ({"venue_checked": False}, "venue_check"),
    ({"halted": "emergency_stop"}, "not_halted"), ({"deployment": None}, "deployment"),
])
def test_each_gate_closes_on_its_own(change, closed):
    args = {"deployment": _dep(**LIMITS), "keys_set": True, "venue_checked": True, "halted": None, **change}
    shut = [g["gate"] for g in setup.live_gates(**args) if not g["ok"]]
    assert shut == ([] if closed is None else [closed] + (["limits"] if closed == "deployment" else []))


def test_the_venue_check_is_not_asked_of_a_venue_it_does_not_apply_to():
    gates = setup.live_gates(deployment=_dep(**LIMITS), keys_set=True, venue_checked=None, halted=None)
    assert all(g["ok"] for g in gates)


def test_the_keys_are_named_never_shown():
    native = setup.key_status(_dep(), environ={"BYBIT_API_KEY": "k"})
    assert native == [{"variable": "BYBIT_API_KEY", "set": True}, {"variable": "BYBIT_API_SECRET", "set": False}]
    okx = setup.key_status(_dep(venue={"exchange": "okx", "environment": "testnet"}), environ={})
    assert [k["variable"] for k in okx] == ["RELAY_OKX_API_KEY", "RELAY_OKX_API_SECRET", "RELAY_OKX_API_PASSPHRASE"]
    assert setup.key_status(_dep(venue={"exchange": "myplugin", "environment": "testnet"}), environ={}) == []


def test_the_catalog_says_what_each_exchange_can_do():
    cat = setup.catalog()
    mexc = next(p for p in cat["profiled"] if p["exchange"] == "mexc")
    assert mexc["can"]["leverage"] is False and mexc["environments"] == ["live"]
    assert cat["native"]["exchange"] == "bybit" and "kraken" in cat["others"]


# ---- through the API -----------------------------------------------------------------------------

@pytest.fixture
async def console(db, tmp_path, monkeypatch):
    (tmp_path / "deployments").mkdir()
    (tmp_path / "deployments" / "main.yaml").write_text("name: main\n")
    for var in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "RELAY_DRY_RUN"):
        monkeypatch.delenv(var, raising=False)
    fake = FakeBybit()
    fake.api_key = "k"
    rt = Runtime(_cfg(config_dir=str(tmp_path)), db=db, client=fake)
    app = create_app(rt)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay",
                                  headers={"Authorization": f"Bearer {KEY}"}) as client):
        yield client, rt, tmp_path


async def test_the_state_follows_the_files_and_says_when_a_restart_is_due(console):
    client, rt, folder = console
    s = (await client.get("/v1/setup")).json()
    assert s["deployment"] == "main" and not s["restart_needed"] and s["config_writable"]
    assert s["missing_limits"] and [k["variable"] for k in s["keys"]] == ["BYBIT_API_KEY", "BYBIT_API_SECRET"]
    r = await client.put("/v1/setup/deployment/main", json={**LIMITS, "venue": {"exchange": "bybit"}})
    assert r.status_code == 200 and r.json()["restart_needed"]
    s = (await client.get("/v1/setup")).json()
    assert s["restart_needed"] and s["missing_limits"] == []
    (e,) = await rt.events.recent(kind="config.changed")
    assert e.severity == "warning"
    assert (await client.get("/v1/setup/deployment/main")).json()["risk"]["max_positions"] == 2


async def test_a_refused_save_says_why(console):
    client, _, folder = console
    r = await client.put("/v1/setup/deployment/main", json={"risk": {"max_positions": "two"}})
    assert r.status_code == 422 and "max_positions" in r.json()["reason"]
    assert _read(folder / "deployments" / "main.yaml") == {"name": "main"}


async def test_the_read_only_key_cannot_change_anything(console):
    client, _, folder = console
    h = {"Authorization": f"Bearer {READ}"}
    assert (await client.get("/v1/setup", headers=h)).status_code == 200
    for method, path, body in (("PUT", "/v1/setup/deployment/main", {}), ("DELETE", "/v1/setup/source/x", None),
                               ("POST", "/v1/setup/live", {"confirm": "main"}), ("POST", "/v1/setup/paper", None),
                               ("POST", "/v1/setup/restart", None), ("POST", "/v1/setup/test-signal", {})):
        r = await client.request(method, path, json=body, headers=h)
        assert r.status_code == 403, (method, path)


async def test_going_live_needs_the_name_and_every_gate(console, monkeypatch):
    client, rt, folder = console
    r = await client.post("/v1/setup/live", json={"confirm": "nope"})
    assert r.status_code == 422
    r = await client.post("/v1/setup/live", json={"confirm": "main"})
    assert r.status_code == 409 and {g["gate"] for g in r.json()["gates"]} == {"keys", "limits"}
    assert _read(folder / "deployments" / "main.yaml").get("dry_run") is not False
    await client.put("/v1/setup/deployment/main", json=LIMITS)
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    r = await client.post("/v1/setup/live", json={"confirm": "main"})          # a key without its secret
    assert r.status_code == 409 and [g["gate"] for g in r.json()["gates"]] == ["keys"]
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    r = await client.post("/v1/setup/live", json={"confirm": "main"})
    assert r.status_code == 200 and r.json() == {"live": True, "restart_needed": True, "env_says_paper": True}
    assert _read(folder / "deployments" / "main.yaml")["dry_run"] is False
    assert rt.is_dry_run()                                     # nothing changes before a restart
    (e,) = [x for x in await rt.events.recent(kind="config.changed") if x.severity == "critical"]
    assert "LIVE" in e.message


async def test_back_to_paper_is_at_once(console):
    client, rt, folder = console
    rt.cfg.dry_run = False
    rt.deployment = rt.deployment.model_copy(update={"dry_run": False})
    assert not rt.is_dry_run()
    r = await client.post("/v1/setup/paper")
    assert r.json() == {"paper": True, "file": True} and rt.is_dry_run()
    assert _read(folder / "deployments" / "main.yaml")["dry_run"] is True


async def test_a_test_signal_runs_on_paper_under_its_own_source(console):
    client, rt, _ = console
    r = await client.post("/v1/setup/test-signal", json={"action": "open", "symbol": "BTCUSDT", "side": "long",
                                                         "size": {"notional_usd": 500}, "stop": {"price": 95}})
    assert r.status_code == 202
    while rt._signal_tasks:
        import asyncio
        await asyncio.gather(*list(rt._signal_tasks))
    row = (await client.get(f"/v1/signals/{r.json()['signal']}")).json()
    assert row["source"] == "console" and row["payload"]["dry_run"] is True and row["status"] == "executed"
    bad = await client.post("/v1/setup/test-signal", json={"action": "open"})
    assert bad.status_code == 422


@pytest.mark.parametrize("body, reason", [
    ({"symbol": "BTCUSDT", "notional": 5, "stop_pct": 2, "environment": "live"}, "test account only"),
    ({"symbol": "BTC/USDT", "notional": 5, "stop_pct": 2, "environment": "testnet"}, "a symbol like"),
    ({"symbol": "BTCUSDT", "notional": 0, "stop_pct": 2, "environment": "testnet"}, "above 0"),
    ({"symbol": "BTCUSDT", "environment": "testnet"}, "give symbol"),
])
async def test_the_venue_check_refuses_what_it_cannot_run_safely(console, body, reason):
    client, rt, folder = console
    native = await client.post("/v1/setup/venue-check", json=body)
    assert native.status_code == 422 and "native Bybit" in native.json()["reason"]
    await client.put("/v1/setup/deployment/main", json={"venue": {"exchange": "okx", "environment": "testnet"}})
    r = await client.post("/v1/setup/venue-check", json=body)
    assert r.status_code == 422 and reason in r.json()["reason"]


async def test_a_venue_check_is_recorded_and_only_one_runs(console, monkeypatch):
    import asyncio

    from relay.venue import factory, verify
    from tests.test_engine import _real_sleep
    client, rt, _ = console
    await client.put("/v1/setup/deployment/main", json={"venue": {"exchange": "okx", "environment": "testnet"}})
    gate = asyncio.Event()

    class Venue:
        async def prepare(self):
            return []

    async def fake_verify(venue, symbol, notional, stop_pct, rebuild, *, can_ladder):
        await gate.wait()
        return [verify.Step("open", True, "ok")]
    monkeypatch.setattr(factory, "build_ccxt_venue", lambda *a, **kw: Venue())
    monkeypatch.setattr(factory, "close_client", lambda v: asyncio.sleep(0))
    monkeypatch.setattr(verify, "verify_venue", fake_verify)
    body = {"symbol": "BTCUSDT", "notional": 5, "stop_pct": 2, "environment": "testnet"}
    assert (await client.post("/v1/setup/venue-check", json=body)).json()["started"]
    assert (await client.post("/v1/setup/venue-check", json=body)).status_code == 409
    assert (await client.get("/v1/setup")).json()["venue_check"]["status"] == "running"
    gate.set()
    for _ in range(100):
        await _real_sleep(0.02)
        check = (await client.get("/v1/setup")).json()["venue_check"]
        if check["status"] != "running":
            break
    assert check["status"] == "passed" and check["steps"] == [{"name": "open", "ok": True, "detail": "ok"}]
    (e,) = await rt.events.recent(kind="venue.checked")
    assert "passed" in e.message


async def test_restart_stops_the_relay_the_normal_way(console, monkeypatch):
    import os
    import signal
    client, _, _ = console
    import asyncio
    sent = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.set_result((pid, sig)))
    r = await client.post("/v1/setup/restart")
    assert r.json()["restarting"], r.json()
    assert await asyncio.wait_for(sent, 5) == (os.getpid(), signal.SIGTERM)


async def test_restart_is_refused_when_the_relay_would_not_start_again(console, monkeypatch, tmp_path):
    import os
    client, _, _ = console
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(sig))
    monkeypatch.delenv("RELAY_SOURCE_BOT_SECRET", raising=False)
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "bot.yaml").write_text(           # its secret is not set yet
        "auth: hmac\nsecret_env: RELAY_SOURCE_BOT_SECRET\ndeployments: [main]\n")
    r = await client.post("/v1/setup/restart")
    assert r.status_code == 409 and not r.json()["restarting"]
    assert any(p.startswith("sources:") and "RELAY_SOURCE_BOT_SECRET" in p for p in r.json()["problems"])
    await _real_sleep(0.7)
    assert killed == []                                       # still running, still watching positions


async def test_sources_and_sinks_are_listed_with_whether_their_variables_are_set(console, monkeypatch):
    client, _, _ = console
    monkeypatch.setenv("RELAY_SOURCE_BOT_SECRET", "s" * 40)
    await client.put("/v1/setup/source/bot", json={"auth": "hmac", "secret_env": "RELAY_SOURCE_BOT_SECRET",
                                                   "deployments": ["main"]})
    await client.put("/v1/setup/sink/ops", json={"type": "webhook", "url_env": "RELAY_SINK_OPS_URL"})
    s = (await client.get("/v1/setup")).json()
    (src,) = s["sources"]
    (snk,) = s["sinks"]
    assert src["variables"] == {"RELAY_SOURCE_BOT_SECRET": True} and not src["running"]
    assert snk["variables"] == {"RELAY_SINK_OPS_URL": False} and s["restart_needed"]
    assert (await client.delete("/v1/setup/source/bot")).json()["removed"]
    assert (await client.delete("/v1/setup/source/bot")).status_code == 422


async def test_saving_a_live_file_in_a_way_that_needs_new_gates_says_it_is_back_on_paper(console, monkeypatch):
    client, rt, folder = console
    await client.put("/v1/setup/deployment/main", json=LIMITS)
    setup.set_dry_run(folder, False)
    r = await client.put("/v1/setup/deployment/main", json={"risk": {"max_positions": 2}})
    assert r.json()["back_to_paper"] is True and _read(folder / "deployments" / "main.yaml")["dry_run"] is True
    assert any("back on paper" in e.message for e in await rt.events.recent(kind="config.changed"))


async def test_two_venue_checks_at_once_start_one(console, monkeypatch):
    import asyncio

    from relay.venue import factory, verify
    client, rt, _ = console
    await client.put("/v1/setup/deployment/main", json={"venue": {"exchange": "okx", "environment": "testnet"}})
    gate = asyncio.Event()

    class Venue:
        async def prepare(self):
            return []

    async def fake_verify(*a, **kw):
        await gate.wait()
        return [verify.Step("open", True, "ok")]
    monkeypatch.setattr(factory, "build_ccxt_venue", lambda *a, **kw: Venue())
    monkeypatch.setattr(factory, "close_client", lambda v: asyncio.sleep(0))
    monkeypatch.setattr(verify, "verify_venue", fake_verify)
    body = {"symbol": "BTCUSDT", "notional": 5, "stop_pct": 2, "environment": "testnet"}
    a, b = await asyncio.gather(client.post("/v1/setup/venue-check", json=body),
                                client.post("/v1/setup/venue-check", json=body))
    assert sorted([a.status_code, b.status_code]) == [202, 409]
    gate.set()


async def test_a_venue_check_cut_off_says_so_and_errors_carry_no_address(console, monkeypatch):
    import asyncio

    from relay.db.models import VenueCheck
    from relay.venue import factory
    from tests.test_engine import _real_sleep
    client, rt, _ = console
    await client.put("/v1/setup/deployment/main", json={"venue": {"exchange": "okx", "environment": "testnet"}})

    class Venue:
        async def prepare(self):
            raise ConnectionError("okx GET https://www.okx.com/api/v5/x?AccessKeyId=SECRETKEY&Signature=abc timed out")
    monkeypatch.setattr(factory, "build_ccxt_venue", lambda *a, **kw: Venue())
    monkeypatch.setattr(factory, "close_client", lambda v: asyncio.sleep(0))
    body = {"symbol": "BTCUSDT", "notional": 5, "stop_pct": 2, "environment": "testnet"}
    await client.post("/v1/setup/venue-check", json=body)
    for _ in range(100):
        check = (await client.get("/v1/setup")).json()["venue_check"]
        if check["status"] != "running":
            break
        await _real_sleep(0.02)
    assert check["status"] == "failed" and "SECRETKEY" not in str(check) and "okx.com" not in str(check)

    running = asyncio.Event()

    class Hangs:
        async def prepare(self):
            running.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(factory, "build_ccxt_venue", lambda *a, **kw: Hangs())
    started = (await client.post("/v1/setup/venue-check", json=body)).json()["check"]
    await asyncio.wait_for(running.wait(), 5)
    for task in list(client._transport.app.state.checks):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with rt.db.session() as s:
        row = await s.get(VenueCheck, started)
    assert row.status == "interrupted" and "close it by hand" in row.steps[-1]["detail"]


async def test_the_venue_check_leaves_a_symbol_the_relay_holds_alone(console):
    from datetime import UTC, datetime

    from relay.db.models import Position
    client, rt, _ = console
    await client.put("/v1/setup/deployment/main", json={"venue": {"exchange": "okx", "environment": "testnet"}})
    async with rt.db.session() as s:
        s.add(Position(deployment_id=rt.deployment_id, symbol="BTCUSDT", side="LONG", size=1, entry_price=100,
                       status="OPEN", dry_run=False, opened_at=datetime.now(UTC)))
        await s.commit()
    r = await client.post("/v1/setup/venue-check", json={"symbol": "BTCUSDT", "notional": 5, "stop_pct": 2,
                                                        "environment": "testnet"})
    assert r.status_code == 409 and "holds BTCUSDT" in r.json()["reason"]


async def test_a_check_left_running_by_a_crash_is_closed_at_the_next_start(db, tmp_path):
    from datetime import UTC, datetime

    from relay.db.models import VenueCheck
    await db.create_all()
    async with db.session() as s:
        s.add(VenueCheck(exchange="okx", market="perpetual", environment="testnet", symbol="BTCUSDT",
                         status="running", created_at=datetime.now(UTC)))
        await s.commit()
    rt = Runtime(_cfg(config_dir=str(tmp_path)), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with db.session() as s:
            (row,) = (await s.execute(__import__("sqlalchemy").select(VenueCheck))).scalars().all()
        assert row.status == "interrupted" and "close it by hand" in row.steps[-1]["detail"]
