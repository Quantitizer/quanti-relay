"""GET /v1/sources/<name>/limits: the owner's limits, for a signing source (a platform) to read
before it builds a trade. Signed like a signal, over "<t>.GET <path>"; no API key."""
import time

import httpx
import pytest

from relay.api.app import create_app
from relay.runtime import Runtime
from relay.signals.sources import sign
from tests.test_api import KEY, _cfg
from tests.test_engine import FakeBybit, _no_sleep  # noqa: F401  (autouse fixture)

SECRET = "k" * 40
OTHER = "o" * 40
DEPLOYMENT = """\
name: main
dry_run: true
risk:
  max_positions: 3
  daily_loss_limit_pct: 2
  max_stop_distance_pct: 5
position_management:
  max_hold_hours: 24
rails:
  max_notional_usd: 500
  on_limit: reject
venue:
  exchange: bybit
"""


@pytest.fixture
async def relay(db, tmp_path, monkeypatch):
    (tmp_path / "deployments").mkdir()
    (tmp_path / "deployments" / "main.yaml").write_text(DEPLOYMENT)
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "quanti.yaml").write_text("auth: hmac\nsecret_env: QUANTI_SECRET\ndeployments: [main]\n")
    (sources / "elsewhere.yaml").write_text("auth: hmac\nsecret_env: OTHER_SECRET\ndeployments: [desk]\n")
    (sources / "off.yaml").write_text("auth: hmac\nsecret_env: OTHER_SECRET\ndeployments: [main]\nenabled: false\n")
    (sources / "tv.yaml").write_text("auth: passphrase\nsecret_env: OTHER_SECRET\nallow_ips: [192.0.2.1]\n"
                                     "deployments: [main]\n")
    monkeypatch.setenv("QUANTI_SECRET", SECRET)
    monkeypatch.setenv("OTHER_SECRET", OTHER)
    rt = Runtime(_cfg(config_dir=str(tmp_path)), db=db, client=FakeBybit())
    app = create_app(rt)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as client):
        yield client, rt


async def _get(client, name="quanti", secret=SECRET, signed=None, t=None, **headers):
    path = f"/v1/sources/{name}/limits"
    header = sign(secret, (signed or f"GET {path}").encode(), t)
    return await client.get(path, headers={"X-Relay-Signature": header, **headers})


async def test_a_signed_read_gets_the_owners_limits_and_off_rules_as_null(relay):
    client, _ = relay
    r = await _get(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["schema"], body["source"], body["deployment"], body["mode"]) == (1, "quanti", "main", "paper")
    assert body["halted"] is False and body["entries_enabled"] is True and body["as_of"]
    assert body["venue"]["exchange"] == "bybit" and body["venue"]["market"] == "perpetual"
    assert body["limits"] == {
        "max_notional_usd": 500, "max_notional_pct": None, "max_gross_leverage": None, "on_limit": "reject",
        "max_positions": 3, "max_exposure_pct": None, "daily_loss_limit_pct": 2, "min_stop_distance_pct": None,
        "max_stop_distance_pct": 5, "max_hold_hours": 24}


@pytest.mark.parametrize("how", ["no signature", "wrong secret", "old", "another path", "a signal body",
                                 "unknown source", "passphrase source", "api key only"])
async def test_anything_but_the_sources_own_fresh_signature_gets_one_answer(relay, how):
    client, _ = relay
    if how == "no signature":
        r = await client.get("/v1/sources/quanti/limits")
    elif how == "api key only":
        r = await client.get("/v1/sources/quanti/limits", headers={"Authorization": f"Bearer {KEY}"})
    else:
        r = await _get(client, **{
            "wrong secret": {"secret": OTHER},
            "old": {"t": int(time.time()) - 120},
            "another path": {"signed": "GET /v1/sources/elsewhere/limits"},
            "a signal body": {"signed": '{"source": "quanti"}'},
            "unknown source": {"name": "nobody"},
            "passphrase source": {"name": "tv", "secret": OTHER},
        }[how])
    assert r.status_code == 401
    assert r.json()["detail"] == "signature not accepted (unknown source, or a bad or old signature)"


async def test_a_disabled_or_unbound_source_is_refused(relay):
    client, _ = relay
    assert (await _get(client, name="off", secret=OTHER)).status_code == 403
    r = await _get(client, name="elsewhere", secret=OTHER)
    assert r.status_code == 403 and "may not reach deployment main" in r.json()["detail"]


async def test_after_an_emergency_stop_it_says_halted(relay):
    client, _ = relay
    stop = await client.post("/v1/emergency-stop", headers={"Authorization": f"Bearer {KEY}"})
    assert stop.status_code == 200
    assert (await _get(client)).json()["halted"] is True
