"""The checks a plugin must pass before the relay can rely on it (docs/plugins/).

Call them from your plugin package's tests; each raises AssertionError saying what is wrong:

    from relay.plugins.contract import check_source, check_sink, check_venue

    def test_my_source():
        check_source(MySource(), samples=[...], unreadable=[...])

    async def test_my_sink():
        await check_sink(MySink, settings={"url_env": "X_URL"}, environ={"X_URL": "https://..."})

    async def test_my_venue():
        await check_venue(build, environment="testnet", environ={}, symbol="BTCUSDT",
                          notional=..., stop_pct=...)

check_venue trades: on an in-memory venue (like the template) that is harmless; for a real
exchange, point it at a test account only, or use `relay verify-venue`, which runs the same steps.
The size and the stop distance are yours to choose - the checks preset none.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import httpx

from relay.plugins import VENUE_CALLS

_EVENT = {"id": 1, "kind": "position.opened", "severity": "warning", "message": "BTCUSDT long opened",
          "data": {"symbol": "BTCUSDT"}, "position_id": 7, "created_at": "2026-11-03T12:00:00+00:00"}


def check_source(plugin: Any, samples: list[dict], unreadable: list[dict]) -> None:
    """Every sample translates into a valid v1 signal, the same way every time, without changing
    its input; every unreadable payload is refused with ValueError (the relay answers 422)."""
    from pydantic import ValidationError

    from relay.signals.contract import parse_signal
    assert samples, "give at least one payload the plugin reads"
    assert isinstance(getattr(plugin, "name", None), str) and plugin.name, "the plugin needs a name"
    for payload in samples:
        before = copy.deepcopy(payload)
        first = plugin.translate(payload)
        assert payload == before, "translate changed the payload it was given"
        assert plugin.translate(copy.deepcopy(payload)) == first, "translate gave two answers for one payload"
        assert isinstance(first, dict), f"translate returned {type(first).__name__}, not a signal object"
        json.dumps(first)                                     # plain JSON, nothing the log cannot store
        try:
            parse_signal({**first, "source": "contract-check"})
        except ValidationError as e:
            raise AssertionError(f"{payload} became an invalid v1 signal: {e}") from None
    for payload in unreadable:
        try:
            plugin.translate(copy.deepcopy(payload))
        except ValueError:
            continue
        except Exception as e:
            raise AssertionError(f"{payload} raised {type(e).__name__}, not ValueError: {e}") from None
        raise AssertionError(f"{payload} was translated, but should have been refused")


async def check_sink(plugin_cls: Any, settings: dict[str, Any], environ: dict[str, str]) -> None:
    """The sink is built from its settings, refuses settings without its variables, delivers an
    event, and says how a failure went: a refusal (4xx) as refused, a receiver that is down
    (5xx, no answer) or busy (429, with its Retry-After) as retried. Its secrets never reach a
    log line. Written for sinks that deliver over HTTP through the `http` client they are given."""
    from relay.sinks import SendError
    sink = plugin_cls.from_config("contract-check", dict(settings), dict(environ))
    try:
        plugin_cls.from_config("contract-check", dict(settings), {})
    except ValueError:
        pass
    else:
        raise AssertionError("from_config accepted settings whose environment variables are not set")

    async def answer(status: int, headers: dict | None = None, fail: bool = False):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if fail:
                raise httpx.ConnectError("contract-check: no answer")
            return httpx.Response(status, headers=headers or {}, text="contract-check")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            try:
                await sink.send(http, dict(_EVENT))
                return None, seen
            except SendError as e:
                return e, seen

    err, seen = await answer(200)
    assert err is None and seen, f"a 200 answer should deliver the event (got {err!r}, {len(seen)} request(s))"
    for status, refused in ((400, True), (404, True), (500, False), (503, False), (408, False)):
        err, _ = await answer(status)
        assert isinstance(err, SendError), f"a {status} answer should raise SendError"
        assert err.refused is refused, f"a {status} answer should be refused={refused}"
    err, _ = await answer(429, {"Retry-After": "7"})
    assert isinstance(err, SendError) and not err.refused and err.retry_after == 7, \
        "a 429 answer should be retried after its Retry-After"
    err, _ = await answer(0, fail=True)
    assert isinstance(err, SendError) and not err.refused, "no answer should raise SendError and be retried"
    secrets = [environ[v] for k, v in settings.items() if k.endswith("_env") and environ.get(v)]
    for secret in secrets:
        text = sink.redact(f"failed at {secret} with {secret}")
        assert secret not in text, "redact left a secret in the text"


async def check_venue(build: Any, *, environment: str, environ: dict[str, str], symbol: str,
                      notional: float, stop_pct: float, rebuild: Any = None) -> list:
    """The venue client has every call the relay makes and a usable venue_id, and passes the
    steps of relay verify-venue (open, stop, move, ladder, restart, close) on `symbol`. The
    restart step builds a second client (`rebuild()`, by default `build` again), which must find
    the position and its stop on the venue."""
    from relay.venue import _ID
    from relay.venue.verify import ladder_offered, passed, render, verify_venue
    venue = build(environment, paper=False, environ=environ)
    missing = [c for c in VENUE_CALLS if not callable(getattr(venue, c, None))]
    assert not missing, f"the venue lacks the call(s) {', '.join(missing)}"
    assert isinstance(getattr(venue, "venue_id", None), str) and _ID.match(venue.venue_id), \
        "venue_id must be 2-30 lowercase letters and digits"
    assert isinstance(getattr(venue, "can_stop", True), bool) and isinstance(getattr(venue, "can_short", True), bool)
    assert isinstance(await venue.prepare(), list), "prepare() returns a list of warnings"

    async def fresh():
        return rebuild() if rebuild is not None else build(environment, paper=False, environ=environ)
    steps = await verify_venue(venue, symbol, notional, stop_pct, fresh,
                               can_ladder=ladder_offered(venue))
    assert passed(steps), "the venue did not pass the steps:\n" + render(steps)
    return steps
