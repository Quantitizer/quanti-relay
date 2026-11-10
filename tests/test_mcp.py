"""The chat-app connector (R7.6): the OAuth sign-in and the MCP endpoint."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from relay.api.app import create_app
from relay.runtime import Runtime
from tests.test_api import KEY, READ, _cfg
from tests.test_engine import FakeBybit, _no_sleep  # noqa: F401  (autouse fixture)

REDIRECT = "https://chat.example/api/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
async def relay(db, tmp_path):
    (tmp_path / "config").mkdir()
    rt = Runtime(_cfg(mcp_enabled=True, public_host="relay.example", auth_max_failures=3,
                      config_dir=str(tmp_path / "config")), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 4000))
        async with httpx.AsyncClient(transport=transport, base_url="https://relay.example") as c:
            yield c, rt


async def _register(c, uris=(REDIRECT,)):
    r = await c.post("/oauth/register", json={"client_name": "Chat", "redirect_uris": list(uris)})
    return r


def _auth_params(client_id, **kw):
    return {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": CHALLENGE,
            "code_challenge_method": "S256", "state": "xyz", "scope": "read setup", **kw}


async def _connect(c, scope="read setup"):
    client_id = (await _register(c)).json()["client_id"]
    tick = {"allow_setup": "1"} if "setup" in scope.split() else {}
    r = await c.post("/oauth/authorize", data={**_auth_params(client_id, scope=scope), "api_key": KEY, **tick})
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["xyz"] and r.headers["location"].startswith(REDIRECT + "?")
    tok = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                             "client_id": client_id, "redirect_uri": REDIRECT,
                                             "code_verifier": VERIFIER})
    assert tok.status_code == 200, tok.text
    return client_id, tok.json(), q["code"][0]


async def _rpc(c, token, method, params=None, id_=1):
    r = await c.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                     json={"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params else {})})
    return r


# ---- discovery and registration -------------------------------------------------------------

async def test_off_by_default(db):
    rt = Runtime(_cfg(), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay") as c:
        for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource"):
            assert (await c.get(path)).status_code == 404
        assert (await c.post("/mcp", json={})).status_code == 404
        assert (await c.post("/oauth/register", json={"redirect_uris": [REDIRECT]})).status_code == 404


async def test_discovery_names_this_relays_public_address(relay):
    c, _ = relay
    meta = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert meta["resource"] == "https://relay.example/mcp" and meta["authorization_servers"] == ["https://relay.example"]
    asm = (await c.get("/.well-known/oauth-authorization-server")).json()
    assert asm["code_challenge_methods_supported"] == ["S256"] and asm["token_endpoint"].endswith("/oauth/token")
    r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401 and 'resource_metadata="https://relay.example/.well-known/' in r.headers["WWW-Authenticate"]


async def test_registration_takes_https_or_local_callbacks_only(relay):
    c, _ = relay
    assert (await _register(c)).status_code == 201
    assert (await _register(c, ["http://localhost:6274/cb"])).status_code == 201
    for bad in (["http://evil.example/cb"], ["javascript:alert(1)"], [], ["https://x.example/cb#frag"]):
        assert (await _register(c, bad)).status_code == 400, bad


# ---- the owner's approval ------------------------------------------------------------------

async def test_the_page_names_the_app_and_where_it_goes_back_and_escapes_them(relay):
    c, _ = relay
    r = await c.post("/oauth/register", json={"client_name": "<script>x</script>", "redirect_uris": [REDIRECT]})
    page = await c.get("/oauth/authorize", params=_auth_params(r.json()["client_id"]))
    assert page.status_code == 200 and "chat.example" in page.text
    assert "<script>x" not in page.text and "&lt;script&gt;" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert "never switch to live" in page.text


async def test_bad_requests_get_no_page(relay):
    c, _ = relay
    cid = (await _register(c)).json()["client_id"]
    for bad in ({"client_id": "nope"}, {"redirect_uri": "https://other.example/cb"},
                {"code_challenge_method": "plain"}, {"code_challenge": "short"}, {"scope": "trade"},
                {"response_type": "token"}):
        assert (await c.get("/oauth/authorize", params={**_auth_params(cid), **bad})).status_code == 400, bad


async def test_a_wrong_key_is_refused_and_counts_towards_the_lockout(relay):
    c, rt = relay
    cid = (await _register(c)).json()["client_id"]
    for _ in range(3):
        r = await c.post("/oauth/authorize", data={**_auth_params(cid), "api_key": "guess"})
        assert r.status_code == 401
    r = await c.post("/oauth/authorize", data={**_auth_params(cid), "api_key": KEY})
    assert r.status_code == 429
    assert await rt.events.recent(kind="auth.locked")


# ---- tokens --------------------------------------------------------------------------------

async def test_a_code_is_used_once_with_its_verifier(relay):
    c, _ = relay
    cid, tokens, code = await _connect(c)
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "read setup"
    again = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": cid,
                                               "redirect_uri": REDIRECT, "code_verifier": VERIFIER})
    assert again.status_code == 400
    cid2 = (await _register(c)).json()["client_id"]
    r = await c.post("/oauth/authorize", data={**_auth_params(cid2), "api_key": KEY})
    code2 = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    wrong = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code2, "client_id": cid2,
                                               "redirect_uri": REDIRECT, "code_verifier": "w" * 64})
    assert wrong.status_code == 400
    # and the failed try used it up
    right = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code2, "client_id": cid2,
                                               "redirect_uri": REDIRECT, "code_verifier": VERIFIER})
    assert right.status_code == 400


async def test_tokens_are_stored_only_as_hashes(relay):
    c, rt = relay
    _, tokens, _ = await _connect(c)
    from sqlalchemy import select

    from relay.db.models import OAuthToken
    async with rt.db.session() as s:
        stored = {row.token_hash for row in (await s.execute(select(OAuthToken))).scalars()}
    assert tokens["access_token"] not in stored
    assert hashlib.sha256(tokens["access_token"].encode()).hexdigest() in stored


async def test_a_refresh_token_works_once(relay):
    c, _ = relay
    cid, tokens, _ = await _connect(c)
    r = await c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                           "client_id": cid})
    assert r.status_code == 200 and r.json()["access_token"] != tokens["access_token"]
    assert (await _rpc(c, r.json()["access_token"], "ping")).status_code == 200
    # a spent one again means a copy is in someone else's hands: everything of that app ends
    again = await c.post("/oauth/token", data={"grant_type": "refresh_token",
                                               "refresh_token": tokens["refresh_token"]})
    assert again.status_code == 400
    assert (await _rpc(c, r.json()["access_token"], "ping")).status_code == 401
    newer = await c.post("/oauth/token", data={"grant_type": "refresh_token",
                                               "refresh_token": r.json()["refresh_token"]})
    assert newer.status_code == 400


async def test_two_refreshes_at_once_cannot_both_win(relay):
    import asyncio
    c, _ = relay
    cid, tokens, _ = await _connect(c)
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": cid}
    answers = await asyncio.gather(*(c.post("/oauth/token", data=form) for _ in range(4)))
    assert sum(a.status_code == 200 for a in answers) <= 1


async def test_removing_an_app_ends_its_tokens_at_once(relay):
    c, _ = relay
    cid, tokens, _ = await _connect(c)
    assert (await _rpc(c, tokens["access_token"], "ping")).status_code == 200
    listed = await c.get("/v1/oauth/clients", headers={"Authorization": f"Bearer {KEY}"})
    assert [x["client_id"] for x in listed.json()] == [cid]
    assert (await c.get("/v1/oauth/clients")).status_code == 401                  # the owner's view needs the key
    assert (await c.delete(f"/v1/oauth/clients/{cid}", headers={"Authorization": f"Bearer {READ}"})).status_code == 403
    assert (await c.delete(f"/v1/oauth/clients/{cid}", headers={"Authorization": f"Bearer {KEY}"})).status_code == 200
    assert (await _rpc(c, tokens["access_token"], "ping")).status_code == 401
    r = await c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert r.status_code == 400


async def test_an_expired_token_is_refused(relay):
    c, rt = relay
    _, tokens, _ = await _connect(c)
    from datetime import UTC, datetime, timedelta

    from relay.db.models import OAuthToken
    async with rt.db.session() as s:
        row = await s.get(OAuthToken, hashlib.sha256(tokens["access_token"].encode()).hexdigest())
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    assert (await _rpc(c, tokens["access_token"], "ping")).status_code == 401


# ---- MCP -----------------------------------------------------------------------------------

async def test_initialize_and_the_tools_of_each_scope(relay):
    c, _ = relay
    _, full, _ = await _connect(c)
    r = (await _rpc(c, full["access_token"], "initialize", {"protocolVersion": "2025-06-18"})).json()["result"]
    assert r["protocolVersion"] == "2025-06-18" and "never ask the owner for a secret" in r["instructions"]
    names = {t["name"] for t in (await _rpc(c, full["access_token"], "tools/list")).json()["result"]["tools"]}
    assert {"relay_status", "setup_write", "venue_check"} <= names
    assert not names & {"emergency_stop", "close_all", "go_live", "place_order"}
    _, ro, _ = await _connect(c, scope="read")
    names = {t["name"] for t in (await _rpc(c, ro["access_token"], "tools/list")).json()["result"]["tools"]}
    assert "relay_status" in names and "setup_write" not in names
    call = (await _rpc(c, ro["access_token"], "tools/call",
                       {"name": "restart_relay", "arguments": {}})).json()["result"]
    assert call["isError"] and "setup" in call["content"][0]["text"]


async def test_a_tool_runs_the_api_route_with_its_checks(relay, tmp_path):
    c, rt = relay
    _, t, _ = await _connect(c)
    st = (await _rpc(c, t["access_token"], "tools/call", {"name": "relay_status"})).json()["result"]
    assert not st["isError"] and st["structuredContent"]["dry_run"] is True
    bad = (await _rpc(c, t["access_token"], "tools/call",
                      {"name": "setup_write", "arguments": {"kind": "deployment", "name": "main",
                                                            "settings": {"risk": {"max_positions": 1.5}}}})).json()
    assert bad["result"]["isError"] and "max_positions" in bad["result"]["content"][0]["text"]
    ok = (await _rpc(c, t["access_token"], "tools/call",
                     {"name": "setup_write", "arguments": {
                         "kind": "deployment", "name": "main",
                         "settings": {"venue": {"exchange": "okx", "environment": "testnet"}, "dry_run": False}}})
          ).json()["result"]
    assert not ok["isError"]
    import yaml
    written = yaml.safe_load((tmp_path / "config" / "deployments" / "main.yaml").read_text())
    assert written["dry_run"] is True                                          # never live through here


async def test_nothing_is_changed_through_a_chat_app_while_live(relay, tmp_path, monkeypatch):
    c, rt = relay
    _, t, _ = await _connect(c)
    dep = tmp_path / "config" / "deployments"
    dep.mkdir()
    (dep / "main.yaml").write_text("name: main\ndry_run: false\nvenue: {exchange: okx, environment: testnet}\n")
    for name, args in (("setup_write", {"kind": "source", "name": "x", "settings": {}}),
                       ("setup_remove", {"kind": "source", "name": "x"}), ("restart_relay", {})):
        r = (await _rpc(c, t["access_token"], "tools/call", {"name": name, "arguments": args})).json()["result"]
        assert r["isError"] and "live" in r["content"][0]["text"], name
    assert (dep / "main.yaml").read_text().startswith("name: main\ndry_run: false")      # untouched
    # a file that cannot be read counts as live; so does a relay running live with a paper file
    (dep / "main.yaml").write_text("dry_run: [")
    r = (await _rpc(c, t["access_token"], "tools/call", {"name": "restart_relay"})).json()["result"]
    assert r["isError"] and "live" in r["content"][0]["text"]
    (dep / "main.yaml").write_text("name: main\ndry_run: true\n")
    monkeypatch.setattr(rt, "is_dry_run", lambda: False)
    r = (await _rpc(c, t["access_token"], "tools/call", {"name": "setup_remove",
                                                          "arguments": {"kind": "source", "name": "x"}})).json()
    assert r["result"]["isError"] and "live" in r["result"]["content"][0]["text"]
    st = (await _rpc(c, t["access_token"], "tools/call", {"name": "relay_status"})).json()["result"]
    assert not st["isError"]                                                     # reading still works


async def test_a_web_page_of_another_origin_cannot_use_the_endpoint(relay):
    c, _ = relay
    for origin, status in (("https://evil.example", 403), ("https://relay.example", 200)):
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {KEY}", "Origin": origin},
                         json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == status, origin


async def test_the_owner_may_allow_reading_only_and_the_page_lets_the_redirect_through(relay):
    c, _ = relay
    cid = (await _register(c)).json()["client_id"]
    page = await c.get("/oauth/authorize", params=_auth_params(cid))
    assert "form-action 'self' https://chat.example;" in page.headers["Content-Security-Policy"]
    assert 'name="allow_setup"' in page.text
    r = await c.post("/oauth/authorize", data={**_auth_params(cid), "api_key": KEY})        # box not ticked
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    tok = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": cid,
                                             "redirect_uri": REDIRECT, "code_verifier": VERIFIER})
    assert tok.json()["scope"] == "read"
    names = {t["name"] for t in (await _rpc(c, tok.json()["access_token"], "tools/list")).json()["result"]["tools"]}
    assert "setup_write" not in names and "relay_status" in names


async def test_callback_addresses_with_odd_hosts_are_refused(relay):
    c, _ = relay
    for bad in ("https://a.example;script-src *@x/cb", "https://someone@example.com/cb",
                "https://chat.example:port/cb", "https://CHAT.example\t/cb", "https://a b.example/cb",
                "https://a;b.example/cb", "https://a'b.example/cb"):
        assert (await _register(c, [bad])).status_code == 400, bad
    assert (await _register(c, ["https://chat.example:8443/cb"])).status_code == 201


async def test_the_full_api_key_works_as_the_bearer_and_wrong_tokens_count(relay):
    c, _ = relay
    assert (await _rpc(c, KEY, "ping")).status_code == 200
    names = {t["name"] for t in (await _rpc(c, READ, "tools/list")).json()["result"]["tools"]}
    assert "setup_write" not in names
    for _ in range(3):
        assert (await _rpc(c, "wrong", "ping")).status_code == 401
    assert (await _rpc(c, KEY, "ping")).status_code == 429


async def test_a_chat_apps_token_never_clears_the_count_of_wrong_ones(relay):
    c, _ = relay
    _, t, _ = await _connect(c)
    for _ in range(2):
        assert (await _rpc(c, "wrong", "ping")).status_code == 401
        assert (await _rpc(c, t["access_token"], "ping")).status_code == 200
    assert (await _rpc(c, "wrong", "ping")).status_code == 401
    assert (await _rpc(c, t["access_token"], "ping")).status_code == 429


async def test_protocol_edges(relay):
    c, _ = relay
    h = {"Authorization": f"Bearer {KEY}"}
    assert (await c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            ).status_code == 202
    assert (await c.post("/mcp", headers=h, content=b"{nope")).json()["error"]["code"] == -32700
    assert (await c.post("/mcp", headers=h, json=[{"jsonrpc": "2.0"}])).json()["error"]["code"] == -32600
    assert (await _rpc(c, KEY, "resources/list")).json()["error"]["code"] == -32601
    assert (await c.get("/mcp", headers=h)).status_code == 405
    r = (await _rpc(c, KEY, "tools/call", {"name": "nope"})).json()["result"]
    assert r["isError"]
    assert json.loads((await _rpc(c, KEY, "initialize", {"protocolVersion": "1999-01-01"})).text)[
        "result"]["protocolVersion"] == "2025-11-25"


async def test_the_address_allowlist_closes_the_connector_too(db):
    rt = Runtime(_cfg(mcp_enabled=True, api_allow_ips="192.0.2.0/24"), db=db, client=FakeBybit())
    app = create_app(rt)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 1)), base_url="https://relay") as c:
        assert (await _rpc(c, KEY, "ping")).status_code == 403
        assert (await c.post("/oauth/register", json={"redirect_uris": [REDIRECT]})).status_code == 403
        assert (await c.get("/.well-known/oauth-authorization-server")).status_code == 403


async def test_connected_apps_are_capped(relay, monkeypatch):
    from relay.api import oauth
    monkeypatch.setattr(oauth, "CLIENTS_MAX", 2)
    c, _ = relay
    await _connect(c)
    second, _, _ = await _connect(c)
    r = await _register(c)
    assert r.status_code == 400 and "remove" in r.json()["error_description"]
    await c.delete(f"/v1/oauth/clients/{second}", headers={"Authorization": f"Bearer {KEY}"})
    assert (await _register(c)).status_code == 201


async def test_strangers_registering_cannot_keep_the_owners_app_out(relay, monkeypatch):
    from relay.api import oauth
    monkeypatch.setattr(oauth, "CLIENTS_MAX", 2)
    monkeypatch.setattr(oauth, "UNAPPROVED_MAX", 3)
    c, rt = relay
    first = (await _register(c)).json()["client_id"]
    for _ in range(4):                                  # waiting ones take no approved app's place
        assert (await _register(c)).status_code == 201
    assert (await c.get("/oauth/authorize", params=_auth_params(first))).status_code == 400   # the oldest went
    await _connect(c)                                  # the owner's own app still connects
    # one address registers at most REGISTER_PER_ADDRESS an hour
    for _ in range(oauth.REGISTER_PER_ADDRESS):
        await _register(c)
    r = await _register(c)
    assert r.status_code == 429
    transport = httpx.ASGITransport(app=c._transport.app, client=("198.51.100.4", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="https://relay.example") as other:
        assert (await _register(other)).status_code == 201


async def test_a_tool_cannot_reach_another_route_through_its_name(relay):
    c, rt = relay
    for args in ({"kind": "source", "name": "../../positions/1/trailing"},
                 {"kind": "../oauth/clients", "name": "x"}, {"kind": "deployment", "name": "main"},
                 {"kind": "source", "name": "Upper"}):
        r = (await _rpc(c, KEY, "tools/call", {"name": "setup_remove", "arguments": args})).json()["result"]
        assert r["isError"] and "lowercase" in r["content"][0]["text"], args
    r = (await _rpc(c, KEY, "tools/call", {"name": "setup_write", "arguments": {
        "kind": "sink", "name": "..%2F..%2Fx", "settings": {}}})).json()["result"]
    assert r["isError"] and "lowercase" in r["content"][0]["text"]


async def test_tokens_of_one_kind_do_not_pass_as_the_other(relay):
    c, _ = relay
    cid, tokens, _ = await _connect(c)
    assert (await _rpc(c, tokens["refresh_token"], "ping")).status_code == 401
    r = await c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["access_token"],
                                           "client_id": cid})
    assert r.status_code == 400


async def test_a_code_goes_back_only_to_the_address_it_was_given_for(relay):
    c, _ = relay
    r = await c.post("/oauth/register", json={"client_name": "Chat",
                                              "redirect_uris": [REDIRECT, "https://chat.example/other"]})
    cid = r.json()["client_id"]
    r = await c.post("/oauth/authorize", data={**_auth_params(cid), "api_key": KEY})
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    tok = await c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": cid,
                                             "redirect_uri": "https://chat.example/other", "code_verifier": VERIFIER})
    assert tok.status_code == 400


async def test_unapproved_registrations_give_their_place_up_after_an_hour(relay, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from relay.api import oauth
    from relay.db.models import OAuthClient
    monkeypatch.setattr(oauth, "CLIENTS_MAX", 2)
    c, rt = relay
    approved, _, _ = await _connect(c)
    stale = (await _register(c)).json()["client_id"]
    async with rt.db.session() as s:
        for cid in (approved, stale):
            (await s.get(OAuthClient, cid)).created_at = datetime.now(UTC) - timedelta(hours=2)
        await s.commit()
    assert (await _register(c)).status_code == 201                             # the stale one let go
    listed = {x["client_id"] for x in (await c.get("/v1/oauth/clients", headers={"Authorization": f"Bearer {KEY}"})).json()}
    assert approved in listed and stale not in listed
