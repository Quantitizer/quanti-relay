"""Deployment file: config/deployments/<name>.yaml, its checks, and how the runtime uses it."""
import httpx
import pytest

from relay.api.app import create_app
from relay.config import Settings
from relay.deployment_config import DeploymentConfigError, env_settings_ignored, load_deployment
from relay.runtime import Runtime
from tests.test_api import KEY, _cfg
from tests.test_engine import FakeBybit, _no_sleep  # noqa: F401  (autouse fixture)

FULL = """
enabled: true
dry_run: true
leverage: 3
isolated_margin: false
require_stop: true
risk:
  max_positions: 4
  max_exposure_pct: 150
  daily_loss_limit_pct: 3
  min_balance: 100
  cooldown_hours: 2
  min_stop_distance_pct: 0.5
  max_stop_distance_pct: 8
  max_size_shortfall_pct: 10
position_management:
  take_profit_ladder: [{pct: 2, close_pct: 50}, {price: 120, close_pct: 50}]
  trailing_distance_pct: 1.5
  trailing_activate_pct: 2
  break_even_trigger_tp: 1
  break_even_buffer_pct: 0.1
  max_hold_hours: 48
"""


def _write(tmp_path, text, name="main.yaml"):
    folder = tmp_path / "deployments"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text)
    return tmp_path


def test_no_file_means_the_environment_settings(tmp_path):
    assert load_deployment(tmp_path) is None
    (tmp_path / "deployments").mkdir()
    assert load_deployment(tmp_path) is None


def test_a_full_file_becomes_the_engines_settings(tmp_path):
    dep = load_deployment(_write(tmp_path, FULL))
    assert dep.name == "main"
    assert dep.execution_settings() == {
        "enabled": True, "leverage": 3.0, "isolated_margin": False, "require_stop": True,
        "max_positions": 4, "max_exposure_pct": 150.0, "daily_loss_limit_pct": 3.0, "min_balance": 100.0,
        "cooldown_hours": 2.0, "min_stop_distance_pct": 0.5, "max_stop_distance_pct": 8.0,
        "max_size_shortfall_pct": 10.0, "take_profit_pct": None,
        "take_profit_ladder": [{"pct": 2.0, "close_pct": 50.0}, {"price": 120.0, "close_pct": 50.0}],
        "trailing_distance_pct": 1.5, "trailing_activate_pct": 2.0, "break_even_trigger_pct": None,
        "break_even_trigger_r": None, "break_even_trigger_tp": 1, "break_even_buffer_pct": 0.1, "max_hold_hours": 48.0,
        "rails": {"symbols": {"allow": [], "deny": []}, "max_notional_usd": None, "max_notional_pct": None,
                  "max_gross_leverage": None, "on_limit": "clip", "trading_hours": None,
                  "stop_before_bankruptcy": True},
        "target": {"neutralize": False, "min_change_pct": None},
    }


def test_an_empty_file_presets_nothing(tmp_path):
    dep = load_deployment(_write(tmp_path, "", name="quiet.yml"))
    s = dep.execution_settings()
    assert (dep.name, s["enabled"], dep.dry_run, s["require_stop"]) == ("quiet", True, True, True)
    assert all(v is None for k, v in s.items() if k not in ("enabled", "require_stop", "rails", "target"))
    assert s["target"] == {"neutralize": False, "min_change_pct": None}
    rails = s["rails"]
    assert all(v is None for k, v in rails.items() if k not in ("symbols", "on_limit", "stop_before_bankruptcy"))
    assert rails["symbols"] == {"allow": [], "deny": []}


@pytest.mark.parametrize("text, match", [
    ("risk: {max_positions: 0}", "max_positions"),
    ("risk: {max_positions: true}", "max_positions"),
    ("risk: {daily_loss_limit_pct: .nan}", "daily_loss_limit_pct"),
    ("risk: {min_stop_distance_pct: 5, max_stop_distance_pct: 5}", "smaller than max_stop_distance_pct"),
    ("risk: {max_stoploss: 5}", "Extra inputs"),
    ("leverage: -2", "leverage"),
    ("dry_run: no thanks", "dry_run"),
    ("enabled: 0", "enabled"),
    ("position_management: {take_profit_pct: 2, take_profit_ladder: [{pct: 3, close_pct: 50}]}", "not both"),
    ("position_management: {take_profit_ladder: [{pct: 3, close_pct: 80}, {pct: 4, close_pct: 80}]}", "at most 100"),
    ("position_management: {trailing_activate_pct: 2}", "needs a trailing distance"),
    ("position_management: {break_even_trigger_tp: 1}", "needs a take_profit_ladder"),
    ("position_management: {break_even_trigger_pct: 1, break_even_trigger_r: 1}", "set one break-even trigger"),
    ("position_management: {break_even_buffer_pct: 1}", "needs a break-even trigger"),
    ("position_management: {max_hold_hours: 0}", "max_hold_hours"),
    ("name: ''", "name"),
    ("[1, 2]", "mapping"),
    ("risk: [oops", "not valid YAML"),
])
def test_a_file_the_relay_cannot_use_stops_the_start(tmp_path, text, match):
    with pytest.raises(DeploymentConfigError, match=match):
        load_deployment(_write(tmp_path, text))


def test_only_one_deployment_so_far(tmp_path):
    _write(tmp_path, "", name="a.yaml")
    _write(tmp_path, "", name="b.yaml")
    with pytest.raises(DeploymentConfigError, match="only one deployment"):
        load_deployment(tmp_path)


def test_environment_trading_settings_a_file_overrides_are_named():
    assert env_settings_ignored(Settings(_env_file=None)) == []
    assert env_settings_ignored(Settings(_env_file=None, max_positions=3, require_stop=False,
                                         dry_run=True)) == ["require_stop", "max_positions"]


@pytest.mark.parametrize("env_dry, file_dry, paper", [(True, True, True), (True, False, True),
                                                      (False, True, True), (False, False, False)])
def test_paper_unless_both_say_live(tmp_path, env_dry, file_dry, paper):
    rt = Runtime(_cfg(dry_run=env_dry, config_dir=str(tmp_path)))
    rt.deployment = load_deployment(_write(tmp_path, f"dry_run: {str(file_dry).lower()}"))
    assert rt.is_dry_run() is paper


async def _relay(db, config_dir):
    rt = Runtime(_cfg(config_dir=str(config_dir), max_positions=7), db=db, client=FakeBybit())
    return rt, create_app(rt)


async def test_the_runtime_runs_the_file(db, tmp_path):
    _write(tmp_path, "name: desk\nrisk: {max_positions: 2}\n")
    rt, app = await _relay(db, tmp_path)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay",
                                  headers={"Authorization": f"Bearer {KEY}"}) as client):
        status = (await client.get("/v1/status")).json()
        assert (status["deployment"], status["deployment_file"], status["entries_enabled"]) == ("desk", True, True)
        assert rt.execution_settings()["max_positions"] == 2           # the file's, not the environment's 7
        r = await client.post("/v1/orders", json={"symbol": "BTCUSDT", "side": "long",
                                                  "size": {"notional_usd": 500}, "stop_price": 95})
        assert r.status_code == 200
        (pos,) = (await client.get("/v1/positions")).json()
        assert pos["dry_run"] is True


async def test_a_disabled_deployment_takes_no_entries_but_still_closes(db, tmp_path):
    _write(tmp_path, "enabled: true\n")
    rt, app = await _relay(db, tmp_path)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay",
                                  headers={"Authorization": f"Bearer {KEY}"}) as client):
        order = {"symbol": "BTCUSDT", "side": "long", "size": {"notional_usd": 500}, "stop_price": 95}
        pid = (await client.post("/v1/orders", json=order)).json()["result"]["position_id"]
        rt.deployment = rt.deployment.model_copy(update={"enabled": False})
        assert (await client.get("/v1/status")).json()["entries_enabled"] is False
        r = await client.post("/v1/orders", json={**order, "symbol": "ETHUSDT"})
        assert r.status_code == 503 and r.json()["result"]["reason"] == "deployment_disabled"
        assert (await client.post(f"/v1/positions/{pid}/close")).status_code == 200


async def test_a_bad_deployment_file_stops_the_start(db, tmp_path):
    _write(tmp_path, "risk: {max_positions: -1}\n")
    rt, app = await _relay(db, tmp_path)
    with pytest.raises(DeploymentConfigError):
        async with app.router.lifespan_context(app):
            pass


def test_the_example_files_load_and_preset_no_number(monkeypatch):
    from pathlib import Path

    from relay.signals.sources import load_sources
    examples = Path(__file__).resolve().parents[1] / "examples" / "config"
    dep = load_deployment(examples)
    s = dep.execution_settings()
    assert (dep.name, dep.enabled, dep.dry_run) == ("main", True, True)
    numbers = {k: v for k, v in s.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    assert numbers == {} and s["take_profit_ladder"] is None
    assert not any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in s["rails"].values())
    src = load_sources(examples, environ={"RELAY_SOURCE_MYBOT_SECRET": "e" * 64,
                                          "RELAY_QUANTITISER_HMAC_KEY": "q" * 64})["mybot"]
    assert src.may_reach("main") and src.max_signals_per_minute is None


# ---- review round (wave 3) ---------------------------------------------------------------------

def test_a_key_set_twice_stops_the_start(tmp_path):
    with pytest.raises(DeploymentConfigError, match="'dry_run' is set twice"):
        load_deployment(_write(tmp_path, "dry_run: true\nenabled: true\ndry_run: false\n"))
    with pytest.raises(DeploymentConfigError, match="'max_positions' is set twice"):
        load_deployment(_write(tmp_path, "risk:\n  max_positions: 2\n  max_positions: 9\n"))


def test_a_source_key_set_twice_stops_the_start(tmp_path):
    from relay.signals.sources import SourceConfigError, load_sources
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "s.yaml").write_text("auth: hmac\nsecret_env: S\ndeployments: [a]\ndeployments: [b]\n")
    with pytest.raises(SourceConfigError, match="set twice"):
        load_sources(tmp_path, environ={"S": "s" * 40})


def test_empty_sections_mean_all_off(tmp_path):
    dep = load_deployment(_write(tmp_path, "risk:\nposition_management:\nrails:\n  symbols:\n    allow:\n"))
    s = dep.execution_settings()
    assert s["max_positions"] is None and s["rails"]["symbols"] == {"allow": [], "deny": []}


def test_unquoted_times_get_a_helpful_message(tmp_path):
    with pytest.raises(DeploymentConfigError, match='write the time in quotes'):
        load_deployment(_write(tmp_path, "rails: {trading_hours: {start: 08:00, end: 20:00}}"))


async def test_open_positions_under_another_deployment_name_stop_the_start(db, tmp_path):
    from tests.test_engine import _engine, _sig
    eng, _ = await _engine(db, FakeBybit())                           # deployment 'test-deployment'
    assert (await eng.execute_entry(_sig()))["success"]
    _write(tmp_path, "name: renamed\n")
    rt, app = await _relay(db, tmp_path)
    with pytest.raises(DeploymentConfigError, match="open positions belong to deployment"):
        async with app.router.lifespan_context(app):
            pass


async def test_only_closed_positions_under_another_name_do_not_stop_the_start(db, tmp_path):
    from tests.test_engine import _engine, _sig
    eng, _ = await _engine(db, FakeBybit())
    pid = (await eng.execute_entry(_sig()))["position_id"]
    await eng.execute_exit({"id": pid}, reason="test")
    _write(tmp_path, "name: renamed\n")
    rt, app = await _relay(db, tmp_path)
    async with app.router.lifespan_context(app):
        assert rt.deployment_name == "renamed"


def test_the_quantitiser_example_loads():
    from pathlib import Path

    from relay.signals.sources import load_sources
    examples = Path(__file__).resolve().parents[1] / "examples" / "config"
    src = load_sources(examples, environ={"RELAY_SOURCE_MYBOT_SECRET": "e" * 64,
                                          "RELAY_QUANTITISER_HMAC_KEY": "q" * 64})["quantitiser"]
    assert src.may_reach("main") and src.max_signals_per_minute is None
