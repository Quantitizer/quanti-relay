"""REST API.

Public: GET /health and the API documentation (/docs, /openapi.json). Everything else needs an API
key (see relay.api.auth); endpoints that place or close orders need the full key. In dry-run (the
default) every order is simulated: positions are paper positions and nothing reaches the exchange.

HTTP status of an order (POST /v1/orders):
  200  executed (or a duplicate client_id: acknowledged, never executed twice)
  409  the position opened but the market had already passed its stop, so it was closed again
  422  refused by the rules or the order itself (stop/size/limit checks); the body says which
  500  the order filled but could not be recorded (the position import adopts it)
  502  a venue problem (order or stop rejected, no price, a check that could not reach the venue)
  503  the relay is halted (emergency stop), still starting, the deployment is disabled (enabled:
       false), or today's loss is not known yet
Close-all / emergency-stop answer 502 when positions remain open. The body always carries the full
result.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from relay import __version__
from relay.api.auth import APIKeyMiddleware
from relay.api.guard import BodyLimitMiddleware, Guard
from relay.api.mcp import router as mcp_router
from relay.api.oauth import OAuth
from relay.api.oauth import router as oauth_router
from relay.config import Settings
from relay.config import settings as default_settings
from relay.db.models import Position
from relay.execution.error_messages import humanize_order_error
from relay.monitoring.metrics import get_bybit_failures
from relay.runtime import Runtime
from relay.signals.contract import BreakEven, EventSignal, TakeProfitLevel, Trailing, parse_signal
from relay.signals.sources import PASSPHRASE_FIELD, SIGNATURE_HEADER, SIGNATURE_WINDOW_S

# Largest signal body read (a signal is a few hundred bytes; a target book a few kilobytes).
MAX_SIGNAL_BYTES = 64 * 1024
# Signals being carried out at once; beyond it a signal is answered 503 (not logged: resend it).
MAX_SIGNALS_IN_FLIGHT = 64
# Longest a source plugin may take to translate one payload (it is a pure translation: no I/O).
TRANSLATE_TIMEOUT_S = 5.0
# Refused (unauthenticated) requests are logged at most once per this many seconds, with a count of
# the ones in between: anyone can send them, so they must not be able to flood the log.
REFUSAL_LOG_EVERY_S = 10
_refusals = {"last": 0.0, "quiet": 0}


def _log_refusal(name: object, problem: str, now: float | None = None) -> bool:
    """Log one refused request unless one was logged in the last REFUSAL_LOG_EVERY_S; True if logged."""
    now = time.monotonic() if now is None else now
    if now - _refusals["last"] < REFUSAL_LOG_EVERY_S:
        _refusals["quiet"] += 1
        return False
    quiet = _refusals["quiet"]
    _refusals["last"], _refusals["quiet"] = now, 0
    logger.warning(f"[Signal] {str(name)[:50]!r}: refused - {problem}"
                   + (f" ({quiet} more refused since the last line)" if quiet else ""))
    return True


class TakeProfits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    levels: list[TakeProfitLevel] = Field(min_length=1)


class OpenOrder(BaseModel):
    # Unknown fields are refused (a misspelt "take_profit" must not be silently ignored), and so
    # are Infinity / NaN and anything that is not a number where one belongs (strict: `true` would
    # otherwise be read as 1, e.g. a stop at 1.0).
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    symbol: str = Field(examples=["BTCUSDT"], min_length=2, max_length=30)
    side: Literal["long", "short"]
    size: dict[str, float] = Field(examples=[{"notional_usd": "<USDT>"}],
                                   description="exactly one of notional_usd, equity_pct, risk_pct, qty")
    stop_price: float | None = Field(default=None, gt=0)
    take_profit_price: float | None = Field(default=None, gt=0)
    take_profits: list[TakeProfitLevel] | Literal[False] | None = Field(
        default=None, description="optional take-profit ladder (instead of take_profit_price); false = no "
        "take-profit at all (the deployment's are not used)")
    leverage: float | None = Field(default=None, gt=0)
    trailing: Trailing | Literal[False] | None = Field(
        default=None, description="optional trailing stop beside the fixed stop (omitted: the deployment's "
        "trailing settings, if any; false = none for this position)")
    break_even: BreakEven | Literal[False] | None = Field(
        default=None, description="optional break-even rule (omitted: the deployment's break-even settings, "
        "if any; false = none for this position)")
    client_id: str | None = Field(default=None, max_length=100,
                                  description="idempotency key: a repeat is acknowledged, never executed twice")

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not (v.isascii() and v.isalnum()):
            raise ValueError("symbol must be letters and digits only, e.g. BTCUSDT")
        return v

    @model_validator(mode="after")
    def _one_take_profit(self):
        if self.take_profit_price is not None and self.take_profits not in (None, False):
            raise ValueError("set take_profit_price or take_profits, not both")
        if self.take_profits == []:
            raise ValueError("take_profits needs at least one level (false = no take-profit)")
        return self

    @model_validator(mode="after")
    def _one_size_mode(self):
        modes = {"notional_usd", "equity_pct", "risk_pct", "qty"}
        if len(set(self.size) & modes) != 1 or set(self.size) - modes:
            raise ValueError(f"size must contain exactly one of {sorted(modes)}")
        value = next(iter(self.size.values()))
        if not math.isfinite(value) or value <= 0:
            raise ValueError("size must be a positive, finite number")
        return self


class Reduce(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    close_pct: float = Field(gt=0, le=100)


class MoveStop(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    stop_price: float = Field(gt=0)


# How a refused stop move maps to an HTTP status.
_MOVE_STOP_STATUS = {"not_open": 409, "shared_venue_position": 409, "stop_already_breached": 422,
                     "stop_too_far": 422, "invalid": 422, "no_price": 502, "venue_rejected": 502,
                     "not_recorded": 500}


# How an entry outcome maps to an HTTP status (see the module doc).
_RULE_REFUSALS = ("stop_required", "stop_wrong_side", "stop_too_close", "stop_too_far",
                  "take_profit_wrong_side", "position_exists", "cooldown", "risk_check_failed",
                  "position_size_rejected", "qty_below_minimum", "below_min_notional",
                  "size_shortfall_exceeded", "trailing_invalid", "break_even_invalid",
                  "take_profit_invalid")
_NOT_RUNNING = ("engine_halted", "execution_engine_not_initialized", "deployment_disabled")


# Risk-check reasons that mean "could not evaluate" (venue/database down), not "refused by a rule".
_CHECK_FAILURES = ("check failed", "Unable to retrieve")


def _entry_http_status(result: dict) -> int:
    if result.get("success"):
        return 200
    reason = str(result.get("reason") or "")
    if reason.startswith("risk_check_failed"):
        if "Daily loss unknown" in reason:
            return 503
        if any(f in reason for f in _CHECK_FAILURES):
            return 502
    if reason.startswith("db_write_failed"):
        return 500
    if reason.startswith(_RULE_REFUSALS):
        return 422
    if reason.startswith(_NOT_RUNNING):
        return 503
    if reason.startswith("stop_breached_at_fill"):
        return 409
    return 502


def _rt(request: Request) -> Runtime:
    rt = request.app.state.runtime
    if rt is None or rt.engine is None:
        raise HTTPException(503, "relay is starting")
    return rt


def _clean(v: Any) -> Any:
    """JSON-ready output: timestamps as ISO strings, floats trimmed to 12 significant digits (drops
    binary artifacts like 0.04519999999999709; far finer than any venue's precision; stored values
    are untouched), non-finite floats as null. Recurses into dicts and lists."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return float(f"{v:.12g}") if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


CONSOLE_DIR = Path(__file__).resolve().parent.parent / "console"
# The console's page holds no data (it reads the API with the key the user enters) and loads
# nothing from anywhere else: these headers keep it that way.
CONSOLE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _console_file(name: str, media_type: str) -> Response:
    return Response((CONSOLE_DIR / name).read_bytes(), media_type=media_type, headers=CONSOLE_HEADERS)


def _model_out(obj) -> dict:
    """An ORM row with its declared types (booleans as true/false, JSON columns as objects) on
    every database, not the raw driver values (SQLite returns 1/0 and JSON text)."""
    return _clean({c.key: getattr(obj, c.key) for c in obj.__mapper__.column_attrs})


_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_QUERY = re.compile(r"[?&][^\s=&]+=[^\s&]*")


def _scrub(text: str) -> str:
    """An exchange's error text without addresses or query strings: CCXT puts the request's URL in
    its errors, and some exchanges sign with the API key in the query."""
    return _QUERY.sub("", _URL.sub("<address>", text or ""))[:300]


async def _close_unfinished_checks(rt: Runtime) -> None:
    """A venue check still marked running when the relay starts or stops was cut off: say so, and
    where to look (it may have left a position or an order on the test account)."""
    from sqlalchemy import select

    from relay.db.models import VenueCheck
    try:
        async with rt.db.session() as s:
            for row in (await s.execute(select(VenueCheck).where(VenueCheck.status == "running"))).scalars():
                row.status, row.finished_at = "interrupted", datetime.now(UTC)
                row.steps = [*(row.steps or []), {"name": "stopped", "ok": False, "detail":
                             f"the relay stopped during the check: look at the {row.exchange} test account for a "
                             f"{row.symbol} position or order left, and close it by hand"}]
            await s.commit()
    except Exception as e:
        logger.warning(f"[Setup] unfinished venue checks could not be closed: {e}")


def _same_body(row, body: dict) -> bool:
    """Whether a repeated id came with the body it was first stored with (compared as JSON). The
    time it was sent may differ: a sender retrying builds the signal again."""
    def strip(d):
        return {k: v for k, v in (d or {}).items() if k != "sent_at"}
    return strip(json.loads(json.dumps(body, default=str))) == strip(row.payload)


async def _repeat(rt: Runtime, source: str, signal_id: str, row, body: dict) -> JSONResponse:
    """A signal id seen before. The same body is a retry: answered as a duplicate and never carried
    out twice. A different body under that id is a sender's mistake (two alerts in one second with
    an id made from the ticker and the time, say): refused with 409, and said, so it is not lost."""
    if _same_body(row, body):
        return JSONResponse({"id": signal_id, "signal": row.id, "status": "duplicate",
                             "signal_status": row.status, "reasons": row.reasons}, status_code=202)
    msg = (f"{source}: signal id {signal_id} was already used for a different signal - this one was NOT "
           f"carried out; give every signal its own id")
    logger.warning(f"[Signal] {msg}")
    await rt.events.record("signal.conflict", "warning", msg, {"signal": row.id, "source": source, "id": signal_id})
    return JSONResponse({"id": signal_id, "signal": row.id, "status": "conflict", "reasons": [
        "this id was already used for a different signal: it was not carried out (give every signal its own id)"]},
        status_code=409)


async def _find_open_position(rt: Runtime, position_id: int) -> dict:
    for p in await rt.engine._own_open_positions(all_modes=True):
        if p["id"] == position_id:
            return p
    raise HTTPException(404, f"no open position {position_id} for this deployment")


def create_app(runtime: Runtime | None = None, cfg: Settings | None = None) -> FastAPI:
    if runtime is not None and cfg is not None and cfg is not runtime.cfg:
        raise ValueError("pass the settings through the runtime: auth and trading must use the same ones")
    cfg = runtime.cfg if runtime is not None else (cfg or default_settings)
    rt = runtime or Runtime(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await rt.start()
        except BaseException:
            # A start that fails half-way (e.g. the engine after the private feed) must not leak
            # background tasks or the database pool.
            await rt.stop()
            raise
        app.state.runtime = rt
        await _close_unfinished_checks(rt)                # left running by a crash
        try:
            yield
        finally:
            # A venue check cut off cleans up after itself (its position and orders on the test
            # account) before the relay goes.
            for task in list(app.state.checks):
                task.cancel()
            await asyncio.gather(*app.state.checks, return_exceptions=True)
            await _close_unfinished_checks(rt)            # cut off before it began
            await rt.stop()

    app = FastAPI(title="Quanti Relay", version=__version__, lifespan=lifespan, redoc_url=None)
    app.state.runtime = None
    app.state.checks = set()          # venue checks running (relay/setup.py)
    app.state.check_busy = False      # set before the first await: two checks can never both start
    app.state.setup_lock = asyncio.Lock()   # config changes and the live switch, one at a time
    app.state.guard = Guard(cfg)      # a bad address list stops the start here
    oauth = OAuth()
    app.include_router(oauth_router(oauth))      # off unless RELAY_MCP_ENABLED (each route says so)
    app.include_router(mcp_router(oauth))
    app.add_middleware(APIKeyMiddleware, settings=cfg, guard=app.state.guard)
    app.add_middleware(BodyLimitMiddleware, limit=cfg.max_body_bytes)   # outermost: before anything reads

    # Document the key so the /docs page's "Authorize" button sends it with every request.
    def _openapi():
        if app.openapi_schema is None:
            from fastapi.openapi.utils import get_openapi
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {})["securitySchemes"] = {
                "apiKey": {"type": "http", "scheme": "bearer"}}
            schema["security"] = [{"apiKey": []}]
            app.openapi_schema = schema
        return app.openapi_schema
    app.openapi = _openapi

    @app.get("/console", include_in_schema=False)
    @app.get("/console/", include_in_schema=False)
    async def console_page() -> Response:
        return _console_file("index.html", "text/html; charset=utf-8")

    @app.get("/console/app.js", include_in_schema=False)
    async def console_script() -> Response:
        return _console_file("app.js", "text/javascript; charset=utf-8")

    @app.get("/console/style.css", include_in_schema=False)
    async def console_style() -> Response:
        return _console_file("style.css", "text/css; charset=utf-8")

    @app.get("/health")
    async def health(request: Request) -> Any:
        # Open to anyone, so it says only the state: no version, no reason (both in /v1/status).
        r = request.app.state.runtime
        state = {"status": "starting"} if r is None else r.health()
        answer = {"service": "quanti-relay", "status": state["status"]}
        if state["status"] != "ok":
            return JSONResponse(answer, status_code=503)
        return answer

    @app.get("/v1/status")
    async def status(request: Request) -> dict[str, Any]:
        r = _rt(request)
        client = r.client
        ws = r.private_ws
        return {
            "version": __version__,
            "health": r.health(),
            "deployment": r.deployment_name,
            "deployment_file": r.deployment is not None,
            "entries_enabled": r.execution_settings().get("enabled") is not False,
            "dry_run": r.is_dry_run(),
            "venue": r.venue[0],
            "environment": r.venue[1],
            "testnet": r.venue[1] == "testnet",           # the venue's environment, whichever venue
            "demo": r.venue[1] == "demo",
            "engine_running": r.engine.is_running,
            "halted": r.engine.halted_reason,
            "private_feed": None if ws is None else bool(ws.is_healthy()),
            "circuit_breaker": client.get_circuit_breaker_status()
            if hasattr(client, "get_circuit_breaker_status") else None,
        }

    @app.get("/v1/positions")
    async def positions(request: Request,
                        state: Literal["open", "closed", "all"] = Query("open", alias="status"),
                        mode: Literal["paper", "live", "all"] = "all",
                        deployment_id: int | None = None,
                        limit: int = Query(100, ge=1, le=1000),
                        offset: int = Query(0, ge=0)) -> list[dict]:
        """Every position the relay tracks (its own and imported ones). 'open' includes rows whose
        close is being finalised (CLOSING). Newest first; page with limit/offset."""
        r = _rt(request)
        q = select(Position)
        if state == "open":
            q = q.where(Position.status.in_(("OPEN", "CLOSING")))
        elif state == "closed":
            q = q.where(Position.status == "CLOSED")
        if mode != "all":
            q = q.where(Position.dry_run.is_(mode == "paper"))
        if deployment_id is not None:
            q = q.where(Position.deployment_id == deployment_id)
        async with r.db.session() as s:
            rows = (await s.execute(q.order_by(Position.opened_at.desc(), Position.id.desc())
                                    .offset(offset).limit(limit))).scalars().all()
        return [_model_out(p) for p in rows]

    @app.post("/v1/orders")
    async def open_order(request: Request, order: OpenOrder) -> Any:
        r = _rt(request)
        client_id = order.client_id or uuid.uuid4().hex
        row, duplicate = await r.signal_log.record(
            "api", client_id, order.model_dump(), deployment=r.deployment_name,
            kind="event", symbol=order.symbol)
        if duplicate:
            if not _same_body(row, order.model_dump()):
                return await _repeat(r, "api", client_id, row, order.model_dump())
            return {"id": row.id, "status": "duplicate", "signal_status": row.status}
        result = await r.engine.execute_entry({
            "symbol": order.symbol,
            "direction": order.side,
            "size": order.size,
            "stop_price": order.stop_price,
            "take_profit_price": order.take_profit_price,
            "leverage": order.leverage,
            # false = none for this position: an empty spec, which replaces the deployment's.
            "trailing": {} if order.trailing is False else order.trailing.model_dump() if order.trailing else None,
            "break_even": {} if order.break_even is False
            else order.break_even.model_dump(exclude_none=True) if order.break_even else None,
            "take_profits": [] if order.take_profits is False
            else [lv.model_dump(exclude_none=True) for lv in order.take_profits] if order.take_profits else None,
            "signal_id": row.id,
            "reason": "api",
        })
        # The engine records the verdict on the signal row; the answer uses the same word.
        recorded = await r.signal_log.get(row.id)
        body = {"id": row.id, "status": recorded.status if recorded else None,
                "reasons": recorded.reasons if recorded else None, "result": _clean(result)}
        return JSONResponse(body, status_code=_entry_http_status(result))

    @app.post("/v1/positions/{position_id}/close")
    async def close_position(request: Request, position_id: int) -> dict[str, Any]:
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        try:
            status = await r.engine.execute_exit(pos, reason="api")
        except Exception as e:
            if str(e).startswith("Failed to close position"):
                raise HTTPException(502, humanize_order_error(str(e))) from e
            logger.opt(exception=True).error(f"[API] close of position {position_id} failed: {e}")
            raise HTTPException(500, "internal error while closing (see the relay log)") from e
        if status == 'in_progress':
            raise HTTPException(409, "another request is already closing this position")
        async with r.db.session() as s:
            return _model_out(await s.get(Position, position_id))

    @app.post("/v1/positions/{position_id}/reduce")
    async def reduce_position(request: Request, position_id: int, body: Reduce) -> dict[str, Any]:
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.execute_partial_exit(pos, body.close_pct, reason="api")
        if not res.get("success"):
            # A size the venue cannot accept is a problem with the request, not with the exchange.
            raise HTTPException(422 if "sizing" in res else 502, res.get("error") or "reduce failed")
        return _clean(res)

    @app.post("/v1/positions/{position_id}/stop")
    async def move_stop(request: Request, position_id: int, body: MoveStop) -> dict[str, Any]:
        """Move the position's stop (tighter or looser). 422 when the price has already passed it or
        it breaks your max_stop_distance_pct, 409 when the position is being closed or shares its
        venue position with another row; the old stop then stays."""
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.move_stop(pos, body.stop_price, reason="api")
        if not res.get("success"):
            raise HTTPException(_MOVE_STOP_STATUS.get(res.get("code"), 502), res.get("error") or "stop not moved")
        return _clean(res)

    @app.post("/v1/positions/{position_id}/trailing")
    async def set_trailing(request: Request, position_id: int, body: Trailing) -> dict[str, Any]:
        """Set (or replace) the position's trailing stop. The fixed stop stays in place."""
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.set_trailing(pos, body.distance_pct, body.activate_pct, reason="api")
        if not res.get("success"):
            raise HTTPException(_MOVE_STOP_STATUS.get(res.get("code"), 502), res.get("error") or "not set")
        return _clean(res)

    @app.delete("/v1/positions/{position_id}/trailing")
    async def remove_trailing(request: Request, position_id: int) -> dict[str, Any]:
        """Remove the position's trailing stop. The fixed stop stays in place."""
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.set_trailing(pos, None, reason="api")
        if not res.get("success"):
            raise HTTPException(_MOVE_STOP_STATUS.get(res.get("code"), 502), res.get("error") or "not removed")
        return _clean(res)

    @app.post("/v1/positions/{position_id}/break-even")
    async def set_break_even(request: Request, position_id: int, body: BreakEven) -> dict[str, Any]:
        """Arm (or replace) break-even on the position: the relay moves its stop once the trigger is
        reached. The current stop is unchanged until then."""
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.set_break_even(pos, body.trigger_pct, body.trigger_r, body.buffer_pct, reason="api",
                                            trigger_tp=body.trigger_tp)
        if not res.get("success"):
            raise HTTPException(_MOVE_STOP_STATUS.get(res.get("code"), 502), res.get("error") or "not armed")
        return _clean(res)

    @app.delete("/v1/positions/{position_id}/break-even")
    async def remove_break_even(request: Request, position_id: int) -> dict[str, Any]:
        """Remove the position's break-even rule. Its stop stays where it is."""
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.set_break_even(pos, reason="api")
        if not res.get("success"):
            raise HTTPException(_MOVE_STOP_STATUS.get(res.get("code"), 502), res.get("error") or "not removed")
        return _clean(res)

    @app.post("/v1/positions/{position_id}/take-profits")
    async def set_take_profits(request: Request, position_id: int, body: TakeProfits) -> dict[str, Any]:
        """Put a take-profit ladder on the position (replacing any earlier one): each level closes
        its close_pct of the current size at its price. Levels the price has already passed are
        skipped and reported."""
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.set_take_profits(pos, [lv.model_dump(exclude_none=True) for lv in body.levels],
                                              reason="api")
        if not res.get("success"):
            raise HTTPException(_MOVE_STOP_STATUS.get(res.get("code"), 502), res.get("error") or "not set")
        return _clean(res)

    @app.delete("/v1/positions/{position_id}/take-profits")
    async def remove_take_profits(request: Request, position_id: int) -> dict[str, Any]:
        """Cancel the position's take-profit ladder."""
        r = _rt(request)
        pos = await _find_open_position(r, position_id)
        res = await r.engine.set_take_profits(pos, None, reason="api")
        if not res.get("success"):
            raise HTTPException(_MOVE_STOP_STATUS.get(res.get("code"), 502), res.get("error") or "not removed")
        return _clean(res)

    @app.post("/v1/stop-rules")
    async def stop_rules(request: Request) -> dict[str, Any]:
        """Run the take-profit and break-even checks now (they also run every stop_rules_interval_s)."""
        return _clean(await _rt(request).engine.run_stop_rules())

    @app.post("/v1/close-all")
    async def close_all(request: Request) -> Any:
        res = await _rt(request).engine.close_all(reason="close_all")
        return JSONResponse(_clean(res), status_code=200 if res.get("success") else 502)

    @app.post("/v1/emergency-stop")
    async def emergency_stop(request: Request) -> Any:
        """Halt new entries, close every position of this deployment and stop the engine. The halt
        holds across restarts until POST /v1/resume. 502 when positions could not all be closed:
        check them now."""
        res = await _rt(request).engine.close_all(reason="emergency_stop", stop_engine=True)
        return JSONResponse(_clean(res), status_code=200 if res.get("success") else 502)

    @app.post("/v1/resume")
    async def resume(request: Request) -> dict[str, Any]:
        """Lift an emergency stop: new entries are taken again (the engine starts if it stopped)."""
        return _clean(await _rt(request).engine.resume())

    @app.post("/v1/sync")
    async def sync(request: Request) -> dict[str, Any]:
        r = _rt(request)
        imported = await r.engine.position_tracker.import_exchange_positions()
        synced = await r.engine.position_tracker.sync_all_positions()
        return _clean({"import": {k: v for k, v in imported.items() if k != "traceback"}, "sync": synced})

    @app.post("/v1/stop-audit")
    async def stop_audit(request: Request) -> dict[str, Any]:
        return _clean(await _rt(request).engine.audit_stops())

    @app.post("/v1/pnl-audit")
    async def pnl_audit(request: Request) -> dict[str, Any]:
        return _clean(await _rt(request).pnl_audit())

    @app.post("/v1/sources/{source_name}/signals", status_code=202)
    async def receive_source_signal(source_name: str, request: Request) -> Any:
        """As POST /v1/signals, for a sender that cannot put "source" in its JSON: the source is
        named in the path. A source with a format (a source plugin) is sent its own JSON here,
        which the plugin translates into a v1 signal once the sender is checked."""
        return await _receive(request, source_name)

    @app.get("/v1/sources/{source_name}/limits")
    async def source_limits(source_name: str, request: Request) -> Any:
        """The owner's limits in force, for a signing source (a platform) to read before it builds
        a trade: signed like a signal, over "<t>.GET <path>" in place of a body (no API key). null
        means that rule is off. The relay still applies every limit itself; this is only a preview.
        Refused: 401 (unknown source, a passphrase source, or a bad or old signature), 403
        (source disabled, or not bound to the running deployment)."""
        r = _rt(request)
        source = r.sources.get(source_name)
        path = request.url.path
        problem = ("unknown source" if source is None else
                   "a passphrase source cannot sign" if source.auth == "passphrase" else
                   source.verify(request.headers.get(SIGNATURE_HEADER), f"GET {path}".encode()))
        if problem:
            _log_refusal(source_name, problem)
            raise HTTPException(401, "signature not accepted (unknown source, or a bad or old signature)")
        if not source.enabled:
            raise HTTPException(403, f"source {source_name} is disabled")
        if not source.may_reach(r.deployment_name):
            raise HTTPException(403, f"source {source_name} may not reach deployment {r.deployment_name}")
        s = r.execution_settings()
        rails = s.get("rails") or {}
        market = r.deployment.venue.market if r.deployment is not None else "perpetual"
        return _clean({
            "schema": 1, "source": source_name, "deployment": r.deployment_name,
            "as_of": datetime.now(UTC), "mode": "paper" if r.is_dry_run() else "live",
            "halted": bool(r.engine.halted_reason), "entries_enabled": s.get("enabled") is not False,
            "venue": {"exchange": r.venue[0], "market": market, "environment": r.venue[1]},
            "limits": {
                "max_notional_usd": rails.get("max_notional_usd"), "max_notional_pct": rails.get("max_notional_pct"),
                "max_gross_leverage": rails.get("max_gross_leverage"), "on_limit": rails.get("on_limit", "clip"),
                **{k: s.get(k) for k in ("max_positions", "max_exposure_pct", "daily_loss_limit_pct",
                                         "min_stop_distance_pct", "max_stop_distance_pct", "max_hold_hours")},
            },
        })

    @app.post("/v1/signals", status_code=202)
    async def receive_signal(request: Request) -> Any:
        """A v1 signal (schemas/signal.v1.json) from a registered source, signed with its secret
        (header X-Relay-Signature: t=<unix>,v1=<hmac of "<t>.<body>">; no API key needed) - or,
        for a passphrase source (TradingView), carrying "passphrase" in the body and sent from one
        of the source's allow_ips.

        202 {"id", "signal", "status": "accepted" | "duplicate", "reasons"}: it is carried out in the
        background; read the outcome at GET /v1/signals/{signal}. A repeated id is never carried out
        twice. Refused: 400 (not JSON), 401 (unknown source or bad signature), 403 (source disabled
        or not bound to the deployment), 413 (too large), 429 (over the source's
        max_signals_per_minute; not logged), 503 (too many signals being carried out;
        not logged, resend it), 422 (not a valid v1 signal, stale, an
        unknown deployment; the body says why). Event signals and target books are both carried out."""
        return await _receive(request, None)

    async def _receive(request: Request, path_source: str | None) -> Any:
        r = _rt(request)
        # Never more than the limit in memory: a declared size over it is refused unread, and the
        # read stops as soon as it is passed.
        declared = request.headers.get("content-length") or ""
        if declared.isascii() and declared.isdecimal() and int(declared) > MAX_SIGNAL_BYTES:
            raise HTTPException(413, f"a signal is at most {MAX_SIGNAL_BYTES} bytes")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > MAX_SIGNAL_BYTES:
                raise HTTPException(413, f"a signal is at most {MAX_SIGNAL_BYTES} bytes")
        body = bytes(buf)
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise HTTPException(400, "the body is not JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "the body must be a JSON object")
        name = path_source if path_source is not None else data.get("source")
        source = r.sources.get(name) if isinstance(name, str) else None
        # One answer for an unknown source and a bad signature: source names cannot be probed.
        if source is None:
            problem = "unknown source"
        elif source.auth == "passphrase":
            # TradingView cannot sign: its address and the passphrase in the body prove it.
            peer = request.client.host if request.client else None
            if source.client_ip_header:                  # the source's own proxy settings
                client_ip = source.client_address(peer, request.headers.get(source.client_ip_header))
            else:                                         # else the relay's (RELAY_TRUSTED_PROXIES), if any
                client_ip = request.app.state.guard.client(peer, request.headers)
            problem = source.verify_passphrase(data.get(PASSPHRASE_FIELD), client_ip)
            data = {k: v for k, v in data.items() if k != PASSPHRASE_FIELD}   # never read on or stored
        else:
            problem = source.verify(request.headers.get(SIGNATURE_HEADER), body)
        if problem:
            _log_refusal(name, problem)
            raise HTTPException(401, "signature not accepted (unknown source, or a bad or old signature)")
        if not source.enabled:
            raise HTTPException(403, f"source {name} is disabled")
        if path_source is not None and data.get("source", name) != name and source.format is None:
            raise HTTPException(422, f"the body names source {data.get('source')!r}, the path {name!r}")
        try:
            # A plugin's translation runs off the event loop and within a time limit: a slow one
            # must not hold up stop management.
            data = (await asyncio.wait_for(asyncio.to_thread(source.translate, data), TRANSLATE_TIMEOUT_S)
                    if source.format else {**data, "source": name})
        except TimeoutError:
            return JSONResponse({"id": None, "status": "rejected", "reasons": [
                f"the {source.format} format took longer than {TRANSLATE_TIMEOUT_S:g} s to read it"]}, status_code=422)
        except Exception as e:
            return JSONResponse({"id": None, "status": "rejected",
                                 "reasons": [f"the {source.format} format could not read it: {str(e)[:300]}"]},
                                status_code=422)
        try:
            sig = parse_signal(data)
        except ValidationError as e:
            errors = [f"{'.'.join(str(x) for x in err['loc']) or 'signal'}: {err['msg']}" for err in e.errors()]
            return JSONResponse({"id": data.get("id"), "status": "rejected", "reasons": errors}, status_code=422)

        # Checks that need a valid signal: the refusal is logged under its id (a repeat is refused alike).
        now = datetime.now(UTC)
        reasons = []
        if not source.may_reach(sig.deployment):
            reasons.append(f"source {name} may not reach deployment {sig.deployment}")
        elif not source.may_send(sig):
            reasons.append(f"source {name} may not send {getattr(sig, 'action', None) or 'a target book'} "
                           f"(its actions: {', '.join(source.actions)})")
        elif sig.deployment != r.deployment_name:
            reasons.append(f"unknown deployment {sig.deployment}")
        if (now - sig.sent_at).total_seconds() > sig.ttl_s:
            reasons.append(f"stale: sent {sig.sent_at.isoformat()}, older than its ttl_s {sig.ttl_s:g}")
        elif (sig.sent_at - now).total_seconds() > SIGNATURE_WINDOW_S:
            reasons.append("sent_at is in the future (check the sender's clock)")
        if not reasons and len(r._signal_tasks) >= MAX_SIGNALS_IN_FLIGHT:
            return JSONResponse({"id": sig.id, "status": "rejected", "reasons": [
                f"busy: {MAX_SIGNALS_IN_FLIGHT} signals are being carried out - send it again shortly"]},
                status_code=503)
        # A repeat is answered as one and never counted against the source's rate (a retry after a
        # timeout, or a replay of a captured request, must not lock the source out).
        seen = await r.signal_log.get_by_source_id(name, sig.id)
        if seen is not None:
            return await _repeat(r, name, sig.id, seen, data)
        if source.over_rate():
            logger.warning(f"[Signal] {name}: refused - over its max_signals_per_minute "
                           f"{source.max_signals_per_minute}")
            raise HTTPException(429, f"source {name} sends more than its max_signals_per_minute "
                                     f"({source.max_signals_per_minute}); not logged - send it again later")
        row, duplicate = await r.signal_log.record(
            name, sig.id, data, deployment=sig.deployment, kind=sig.kind,
            symbol=getattr(sig, "symbol", None), status="rejected" if reasons else "accepted")
        if duplicate:
            return await _repeat(r, name, sig.id, row, data)
        if reasons:
            await r.signal_log.mark_result(row.id, "rejected", reasons=reasons)
            await r.events.record("signal.rejected", "info", f"{name}: signal {sig.id} rejected - {'; '.join(reasons)}",
                                  {"signal": row.id, "source": name, "reasons": reasons})
            status = 403 if reasons[0].startswith(f"source {name} may not") else 422
            logger.warning(f"[Signal] {name} {sig.id}: rejected - {'; '.join(reasons)}")
            return JSONResponse({"id": sig.id, "signal": row.id, "status": "rejected", "reasons": reasons},
                                status_code=status)
        if isinstance(sig, EventSignal):
            logger.info(f"[Signal] {name} {sig.id}: accepted ({sig.action} {sig.symbol or ''})")
            r.run_signal(r.event_executor.execute(sig, row.id))
        else:
            logger.info(f"[Signal] {name} {sig.id}: accepted (target book of {len(sig.weights)} symbol(s))")
            r.run_signal(r.target_executor.execute(sig, row.id))
        return JSONResponse({"id": sig.id, "signal": row.id, "status": "accepted", "reasons": []}, status_code=202)

    # ---- setting up from the console (relay/setup.py) ---------------------------------------
    async def _setup_state(r: Runtime) -> dict[str, Any]:
        """Built from the files as they are now (what the next start runs), beside what runs now."""
        from relay import setup
        disk = setup.on_disk(r.cfg.config_dir)
        dep = disk["deployment"]
        keys = setup.key_status(dep)
        keys_set = bool(keys) and all(k["set"] for k in keys)
        check = await _latest_check(r, dep)
        native = dep is None or dep.venue.native
        checked = None if native or keys == [] else (check is not None and check["status"] == "passed")
        running = r.deployment.model_dump() if r.deployment is not None else None
        restart_needed = (running != (dep.model_dump() if dep is not None else None)
                          or sorted(x["name"] for x in disk["sources"]) != sorted(r.sources)
                          or sorted(x["name"] for x in disk["sinks"]) != sorted(k.name for k in r.sinks))
        return _clean({
            "deployment": dep.name if dep is not None else r.deployment_name, "deployment_file": dep is not None,
            "deployment_problem": disk["deployment_problem"],
            "config_dir": str(r.cfg.config_dir), "config_writable": setup.writable(r.cfg.config_dir),
            "in_container": os.path.exists("/.dockerenv"), "restart_needed": restart_needed,
            "paper": r.is_dry_run(), "dry_run_file": None if dep is None else dep.dry_run,
            "dry_run_env": bool(r.cfg.dry_run),
            "venue": None if dep is None else dep.venue.model_dump(), "running_venue": list(r.venue),
            "keys": keys, "keys_set": keys_set,
            "settings": None if dep is None else dep.model_dump(),
            "missing_limits": setup.missing_limits(dep),
            "sources": [{"name": x["name"], "auth": x["settings"].get("auth"),
                         "secret_env": x["settings"].get("secret_env"), "variables": x["variables"],
                         "actions": x["settings"].get("actions") or [], "deployments": x["settings"].get("deployments"),
                         "enabled": x["settings"].get("enabled", True), "running": x["name"] in r.sources}
                        for x in disk["sources"]],
            "sinks": [{"name": x["name"], "type": x["settings"].get("type"), "variables": x["variables"],
                       "min_severity": x["settings"].get("min_severity", "info"),
                       "running": x["name"] in {k.name for k in r.sinks}} for x in disk["sinks"]],
            "venue_check": check,
            "live_gates": setup.live_gates(deployment=dep, keys_set=keys_set, venue_checked=checked,
                                           halted=r.engine.halted_reason),
        })

    async def _latest_check(r: Runtime, dep) -> dict | None:
        from sqlalchemy import select

        from relay.db.models import VenueCheck
        if dep is None or dep.venue.native:
            return None
        async with r.db.session() as s:
            row = (await s.execute(select(VenueCheck).where(
                VenueCheck.exchange == dep.venue.exchange, VenueCheck.market == dep.venue.market)
                .order_by(VenueCheck.id.desc()).limit(1))).scalar_one_or_none()
        return None if row is None else _model_out(row)

    def _config_changed(r: Runtime, what: str, data: dict) -> Any:
        return r.events.record("config.changed", "warning", f"the console changed {what}; it applies at the next "
                               f"start (restart the relay)", data)

    @app.get("/v1/setup")
    async def setup_state(request: Request) -> dict[str, Any]:
        """What is set up and what is missing: the deployment, the exchange and its keys (set or not,
        never their values), the limits, sources, sinks, the venue check, and the gates to go live."""
        return await _setup_state(_rt(request))

    @app.get("/v1/setup/catalog")
    async def setup_catalog(request: Request) -> dict[str, Any]:
        """The exchanges on offer and what each can do."""
        from relay import setup
        _rt(request)
        return setup.catalog()

    @app.get("/v1/setup/{kind}/{name}")
    async def setup_read(request: Request, kind: Literal["deployment", "source", "sink"], name: str) -> Any:
        """A config file's settings as they are on disk (for editing)."""
        from relay import setup
        r = _rt(request)
        if not setup.NAME.match(name):
            raise HTTPException(404, "unknown name")
        folder = Path(r.cfg.config_dir) / setup.KINDS[kind]
        for path in (folder / f"{name}.yaml", folder / f"{name}.yml"):
            if path.is_file():
                from relay.deployment_config import read_yaml
                return _clean(read_yaml(path.read_text()) or {})
        raise HTTPException(404, f"no {kind} {name}")

    @app.put("/v1/setup/{kind}/{name}")
    async def setup_write(request: Request, kind: Literal["deployment", "source", "sink"], name: str) -> Any:
        """Check and write a config file (the replaced version is kept). A deployment keeps its
        dry_run here; it applies at the next start."""
        from relay import setup
        r = _rt(request)
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON") from None
        async with request.app.state.setup_lock:          # one change at a time (see setup_live)
            was = setup.on_disk(r.cfg.config_dir)["deployment"]
            try:
                path = setup.write_config(r.cfg.config_dir, kind, name, data)
            except setup.SetupError as e:
                return JSONResponse({"written": False, "reason": str(e)}, status_code=422)
            back = (kind == "deployment" and was is not None and was.dry_run is False
                    and (setup.read_yaml(path.read_text()) or {}).get("dry_run") is True)
        await _config_changed(r, f"the {kind} {name}", {"kind": kind, "name": name})
        if back:
            await r.events.record("config.changed", "warning", "the deployment file is back on paper: the change "
                                  "touched what going live checked (the exchange or a core limit) - go live again",
                                  {"kind": "deployment", "dry_run": True})
        return {"written": True, "file": path.name, "restart_needed": True, "back_to_paper": back}

    @app.delete("/v1/setup/{kind}/{name}")
    async def setup_remove(request: Request, kind: Literal["source", "sink"], name: str) -> Any:
        from relay import setup
        r = _rt(request)
        try:
            async with request.app.state.setup_lock:
                setup.remove_config(r.cfg.config_dir, kind, name)
        except setup.SetupError as e:
            return JSONResponse({"removed": False, "reason": str(e)}, status_code=422)
        await _config_changed(r, f"the {kind} {name} (removed)", {"kind": kind, "name": name, "removed": True})
        return {"removed": True, "restart_needed": True}

    @app.post("/v1/setup/live")
    async def setup_live(request: Request) -> Any:
        """Write dry_run: false, after the gates pass and the deployment's name is typed. The
        environment (RELAY_DRY_RUN) must say live too, and a restart applies it."""
        from relay import setup
        r = _rt(request)
        body = await request.json()
        if not isinstance(body, dict) or body.get("confirm") != r.deployment_name:
            return JSONResponse({"live": False, "reason": f"type the deployment's name ({r.deployment_name}) to "
                                                          f"confirm"}, status_code=422)
        # The gates are read and the switch is written under the lock every change takes: no save
        # can slip in between, so the file that goes live is the file the gates passed.
        async with request.app.state.setup_lock:
            state = await _setup_state(r)
            closed = [g for g in state["live_gates"] if not g["ok"]]
            if closed:
                return JSONResponse({"live": False, "reason": "not every gate is open",
                                     "gates": closed}, status_code=409)
            try:
                setup.set_dry_run(r.cfg.config_dir, False)
            except setup.SetupError as e:
                return JSONResponse({"live": False, "reason": str(e)}, status_code=422)
        await r.events.record("config.changed", "critical",
                              f"the owner set the deployment {r.deployment_name} to LIVE in its file; it trades live "
                              f"after a restart" + (" once RELAY_DRY_RUN is false too" if r.cfg.dry_run else ""),
                              {"kind": "deployment", "dry_run": False})
        return {"live": True, "restart_needed": True, "env_says_paper": bool(r.cfg.dry_run)}

    @app.post("/v1/setup/paper")
    async def setup_paper(request: Request) -> Any:
        """Back to paper, at once (new orders are simulated from now on) and in the file."""
        from relay import setup
        r = _rt(request)
        if r.deployment is not None:
            r.deployment = r.deployment.model_copy(update={"dry_run": True})
            try:
                async with request.app.state.setup_lock:
                    setup.set_dry_run(r.cfg.config_dir, True)
            except setup.SetupError as e:
                await r.events.record("config.changed", "warning", f"paper from now on, but the file could not "
                                      f"say so ({e}): set dry_run: true in it before the next start", {"dry_run": True})
                return {"paper": True, "file": False, "reason": str(e)}
        await r.events.record("config.changed", "warning", "the owner put the deployment on paper",
                              {"kind": "deployment", "dry_run": True})
        return {"paper": r.is_dry_run(), "file": r.deployment is not None}

    @app.post("/v1/setup/restart")
    async def setup_restart(request: Request) -> Any:
        """Stop the relay the normal way (entries in flight finish). Under Docker it starts again by
        itself (restart: unless-stopped); elsewhere start it again yourself. Refused (409) when the
        relay would not start again with the files and variables as they are now: a stopped relay
        watches no positions."""
        r = _rt(request)
        from relay import doctor
        checks, _ = await asyncio.to_thread(doctor.offline_checks, r.cfg)
        # What stops a start. The key's length is not among them: a relay that runs passed that already.
        problems = [f"{c['check']}: {c['detail']}" for c in checks
                    if c["status"] == "fail" and c["check"] not in ("api_key", "api_readonly_key")]
        if problems:
            return JSONResponse({"restarting": False, "problems": problems,
                                 "detail": "the relay would not start again like this: fix these first "
                                           "(relay doctor)"}, status_code=409)
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
        return {"restarting": True, "in_container": os.path.exists("/.dockerenv")}

    @app.post("/v1/setup/test-signal", status_code=202)
    async def setup_test_signal(request: Request) -> Any:
        """A paper signal from the console (source `console`): it runs the whole way through the relay
        - checks, rails, the simulated fill - and never reaches the exchange."""
        r = _rt(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "the body must be a JSON object")
        data = {**body, "version": "1", "id": "console-" + uuid.uuid4().hex[:16], "source": "console",
                "deployment": r.deployment_name, "sent_at": datetime.now(UTC).isoformat(), "dry_run": True}
        if "kind" not in data:
            data["kind"] = "event"
        try:
            sig = parse_signal(data)
        except ValidationError as e:
            errors = [f"{'.'.join(str(x) for x in err['loc']) or 'signal'}: {err['msg']}" for err in e.errors()]
            return JSONResponse({"status": "rejected", "reasons": errors}, status_code=422)
        row, _ = await r.signal_log.record("console", sig.id, data, deployment=sig.deployment, kind=sig.kind,
                                           symbol=getattr(sig, "symbol", None))
        r.run_signal(r.event_executor.execute(sig, row.id) if isinstance(sig, EventSignal)
                     else r.target_executor.execute(sig, row.id))
        return {"status": "accepted", "signal": row.id}

    @app.post("/v1/setup/venue-check", status_code=202)
    async def setup_venue_check(request: Request) -> Any:
        """relay verify-venue on the deployment's exchange, on a test account: it opens a small
        position, sets and moves its stop, closes it and cleans up. The size and the stop distance
        are the owner's. Never on a live account."""
        if request.app.state.check_busy:                 # before any await: no race between two posts
            return JSONResponse({"started": False, "reason": "a venue check is running already"}, status_code=409)
        request.app.state.check_busy = True
        started = False
        try:
            started, answer = await _start_check(request)
            return answer
        finally:
            if not started:
                request.app.state.check_busy = False

    async def _start_check(request: Request) -> tuple[bool, Any]:
        from relay import setup
        r = _rt(request)
        body = await request.json()
        if not isinstance(body, dict):
            return False, JSONResponse({"started": False, "reason": "the body must be an object"}, status_code=422)
        dep = setup.on_disk(r.cfg.config_dir)["deployment"]     # the exchange as set up, before a restart too
        if dep is None or dep.venue.native:
            return False, JSONResponse({"started": False, "reason": "the venue check is for a CCXT exchange: native "
                                                                    "Bybit is checked by the project"}, status_code=422)
        env = body.get("environment") or dep.venue.environment
        try:
            symbol = str(body["symbol"]).upper()
            notional, stop_pct = float(body["notional"]), float(body["stop_pct"])
        except (KeyError, TypeError, ValueError):
            return False, JSONResponse({"started": False, "reason": "give symbol, notional (USDT) and stop_pct"},
                                       status_code=422)
        if env not in ("demo", "testnet"):
            return False, JSONResponse({"started": False, "reason": "the venue check runs on a test account only: "
                                                                    "choose demo or testnet"}, status_code=422)
        if not (notional > 0 and 0 < stop_pct < 100) or not symbol.isalnum():
            return False, JSONResponse({"started": False, "reason": "a symbol like BTCUSDT, a notional above 0 and a "
                                                                    "stop between 0 and 100 %"}, status_code=422)
        held = {p.get("symbol") for p in await r.engine._own_open_positions(all_modes=True) if not p.get("dry_run")}
        if symbol in held:
            # The check closes everything on its symbol, the relay's own stop included.
            return False, JSONResponse({"started": False, "reason": f"the relay holds {symbol}: check another "
                                                                    f"symbol"}, status_code=409)
        from relay.db.models import VenueCheck
        async with r.db.session() as s:
            row = VenueCheck(exchange=dep.venue.exchange, market=dep.venue.market, environment=env, symbol=symbol,
                             status="running", created_at=datetime.now(UTC))
            s.add(row)
            await s.commit()
            check_id = row.id
        task = asyncio.ensure_future(_venue_check(r, check_id, dep.venue.exchange, dep.venue.market, env, symbol,
                                                  notional, stop_pct))
        request.app.state.checks.add(task)

        def done(t):
            request.app.state.checks.discard(t)
            request.app.state.check_busy = False
        task.add_done_callback(done)
        return True, {"started": True, "check": check_id}

    async def _venue_check(r: Runtime, check_id: int, exchange: str, market: str, env: str, symbol: str,
                           notional: float, stop_pct: float) -> None:
        from relay.db.models import VenueCheck
        from relay.venue.factory import build_ccxt_venue, close_client
        from relay.venue.verify import ladder_offered, passed, verify_venue
        steps, status = [], "failed"
        try:
            def build():
                return build_ccxt_venue(exchange, env, paper=False, market=market)
            venue = build()
            try:
                await venue.prepare()

                async def rebuild():
                    return build()
                result = await verify_venue(venue, symbol, notional, stop_pct, rebuild, can_ladder=ladder_offered(venue))
                steps = [{"name": x.name, "ok": x.ok, "detail": _scrub(x.detail)} for x in result]
                status = "passed" if passed(result) else "failed"
            finally:
                await close_client(venue)
        except asyncio.CancelledError:
            # The relay is stopping: verify_venue cleaned up on its way out, but what it found is lost.
            status = "interrupted"
            steps.append({"name": "stopped", "ok": False, "detail": f"the relay stopped during the check: look at the "
                                                                    f"{exchange} test account for a {symbol} position "
                                                                    f"or order left, and close it by hand"})
            raise
        except Exception as e:
            steps.append({"name": "start", "ok": False, "detail": f"{type(e).__name__}: {_scrub(str(e))}"})
        finally:
            async with r.db.session() as s:
                row = await s.get(VenueCheck, check_id)
                row.status, row.steps, row.finished_at = status, steps, datetime.now(UTC)
                await s.commit()
            await r.events.record("venue.checked", "info" if status == "passed" else "warning",
                                  f"venue check on {exchange}{' spot' if market == 'spot' else ''} ({env}): {status}",
                                  {"check": check_id, "status": status})

    @app.get("/v1/events")
    async def events(request: Request, limit: int = Query(100, ge=1, le=1000),
                     kind: str | None = None) -> list[dict]:
        """What the relay told the user about (newest first): e.g. position.drift_detected /
        position.drift_repaired / position.drift_unrepairable."""
        return [_model_out(e) for e in await _rt(request).events.recent(limit, kind)]

    @app.get("/v1/signals")
    async def list_signals(request: Request, limit: int = Query(100, ge=1, le=1000),
                           status: Literal["accepted", "executed", "skipped", "rejected", "failed"] | None = None
                           ) -> list[dict]:
        """The signal log, newest first: what came in, the verdict and the result."""
        return [_model_out(row) for row in await _rt(request).signal_log.recent(limit, status)]

    @app.get("/v1/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        """The trading settings in force (the deployment file's, or the RELAY_* environment's when
        there is no file): read-only here - they are changed in the file, by its owner."""
        r = _rt(request)
        return _clean({"deployment": r.deployment_name, "deployment_file": r.deployment is not None,
                       "dry_run": r.is_dry_run(), "venue": r.venue[0], "environment": r.venue[1],
                       "settings": r.execution_settings()})

    @app.get("/v1/signals/{signal_row_id}")
    async def get_signal(request: Request, signal_row_id: int) -> dict[str, Any]:
        row = await _rt(request).signal_log.get(signal_row_id)
        if row is None:
            raise HTTPException(404, "unknown signal")
        return _model_out(row)

    @app.post("/v1/circuit-breaker/reset")
    async def reset_breaker(request: Request) -> dict[str, Any]:
        client = _rt(request).client
        breaker = getattr(client, "circuit_breaker", None)
        if breaker is None:
            raise HTTPException(404, "no circuit breaker on this client")
        breaker.reset()
        return client.get_circuit_breaker_status()

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> str:
        r = _rt(request)
        async with r.db.acquire() as c:
            counts = await c.fetch(
                "SELECT dry_run, count(*) AS n FROM positions WHERE status IN ('OPEN', 'CLOSING') "
                "GROUP BY dry_run")
        by_mode = {"paper": 0, "live": 0}
        for x in counts:
            by_mode["paper" if x["dry_run"] else "live"] = int(x["n"])
        lines = [
            "# HELP relay_dry_run 1 when orders are simulated.",
            "# TYPE relay_dry_run gauge",
            f"relay_dry_run {int(r.is_dry_run())}",
            "# HELP relay_open_positions Open positions by mode.",
            "# TYPE relay_open_positions gauge",
            *[f'relay_open_positions{{mode="{m}"}} {n}' for m, n in by_mode.items()],
            "# HELP relay_bybit_failures_total Bybit call failures by kind.",
            "# TYPE relay_bybit_failures_total counter",
            *[f'relay_bybit_failures_total{{kind="{k}"}} {v}' for k, v in get_bybit_failures().items()],
        ]
        return "\n".join(lines) + "\n"

    return app
