"""The console: a page with no data of its own, served under a strict policy, reading the API with
the key the user enters."""
import re
from pathlib import Path

import pytest

from tests.test_api import (
    READ,
    _bare,
    _cfg,
    api,  # noqa: F401  (fixture)
)
from tests.test_engine import _no_sleep  # noqa: F401  (autouse fixture)

CONSOLE = Path(__file__).parent.parent / "relay" / "console"


@pytest.mark.parametrize("path, kind", [("/console", "text/html"), ("/console/", "text/html"),
                                        ("/console/app.js", "text/javascript"), ("/console/style.css", "text/css")])
async def test_the_console_files_are_public_and_load_nothing_from_elsewhere(db, path, kind):
    r = await _bare(_cfg(), db, path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(kind)
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


def test_the_console_has_no_outside_scripts_no_inline_code_and_keeps_the_key_in_the_tab():
    html = (CONSOLE / "index.html").read_text()
    js = (CONSOLE / "app.js").read_text()
    assert not re.search(r"https?://", html + js)                     # nothing from anywhere else
    assert "<script>" not in html and not re.search(r"\son[a-z]+\s*=", html)      # no inline code or handlers
    assert "sessionStorage" in js and "localStorage" not in js
    assert "innerHTML" not in js                                       # text only: nothing it shows can run


async def test_the_console_pages_hold_no_data_without_a_key(db):
    for path in ("/v1/signals", "/v1/settings", "/v1/positions"):
        assert (await _bare(_cfg(), db, path)).status_code == 401


async def test_the_signal_log_and_the_settings_read_with_a_key(api):  # noqa: F811
    client, _, rt = api
    await rt.signal_log.record("mybot", "s-1", {"x": 1}, deployment="default", kind="event", symbol="BTCUSDT")
    row, _ = await rt.signal_log.record("mybot", "s-2", {"x": 2}, deployment="default", kind="event")
    await rt.signal_log.mark_result(row.id, "skipped", reasons=["stop_required"])
    rows = (await client.get("/v1/signals")).json()
    assert [r["signal_id"] for r in rows] == ["s-2", "s-1"]
    assert [r["signal_id"] for r in (await client.get("/v1/signals?status=skipped")).json()] == ["s-2"]
    assert (await client.get("/v1/signals?status=nonsense")).status_code == 422
    s = (await client.get("/v1/settings")).json()
    assert s["dry_run"] is True and s["deployment"] == rt.deployment_name and "require_stop" in s["settings"]


async def test_a_read_only_key_sees_but_cannot_stop(db):
    assert (await _bare(_cfg(), db, "/v1/signals", headers={"X-API-Key": READ})).status_code == 200
    assert (await _bare(_cfg(), db, "/v1/emergency-stop", "POST", headers={"X-API-Key": READ})).status_code == 403


def test_the_stop_tab_refuses_while_the_deployment_name_is_unknown():
    js = (CONSOLE / "app.js").read_text()
    form = js[js.index('"stop-form"'):]
    guard, post = form.index("if (!deployment)"), form.index('api("/v1/" + action')
    assert guard < post and "return;" in form[guard:post]               # an empty name never matches ""
