"""The relay's MCP endpoint (`POST /mcp`): a chat app the owner connected (relay.api.oauth) reads
the relay and sets it up through these tools. Off unless RELAY_MCP_ENABLED.

It speaks MCP's Streamable HTTP transport in its plain form: one JSON-RPC message per POST, one JSON
answer, no event stream and no session. Every tool calls the relay's own API inside the process, so
a tool can do no more than the API route it names, with the same checks, locks and events. The
tools are the ones setup and daily watching need; going live, the emergency stop, closing
positions and placing orders are not among them. While the relay runs live, or a deployment file
says live, the tools change nothing at all (LIVE_LOCKED): a live relay is changed by its owner in
the console, never by a chat app.

Access: an OAuth access token (scopes `read`, `setup`) or the relay's full API key as the bearer
(for agents the owner configures by hand). The read-only API key may use the `read` tools.
Wrong tokens count towards the address lockout like wrong API keys.
"""
from __future__ import annotations

import hmac
import json
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from relay import __version__, setup
from relay.api.oauth import OAuth, base_url

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
INSTRUCTIONS = (
    "This is the owner's own Quanti Relay, a self-hosted relay that turns trading signals into exchange "
    "orders behind risk rails. Use the tools to read its state and set it up. Rules: never ask the owner for "
    "a secret (exchange keys, passphrases, tokens) in the chat: secrets are set on the server with "
    "`relay secret set <NAME>` at the owner's own terminal. Never suggest trading numbers (sizes, stops, "
    "leverage, limits): ask the owner for each. Going live is the owner's own step in the console; these "
    "tools cannot do it. After a settings change, call restart_relay for it to take effect. The setup "
    "playbook: docs/setup-playbook.md in the relay's repository.")

_OBJ = {"type": "object", "additionalProperties": False}
LIVE_LOCKED = {"setup_write", "setup_remove", "restart_relay"}
KIND = {"type": "string", "enum": ["deployment", "source", "sink"]}
NAME = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,49}$"}

# name -> (scope, description, input schema, (method, path template), read-only)
TOOLS: dict[str, tuple[str, str, dict, tuple[str, str], bool]] = {
    "relay_status": ("read", "The relay's state: paper or live, the exchange, whether it runs, halted or not.",
                     {**_OBJ, "properties": {}}, ("GET", "/v1/status"), True),
    "list_positions": ("read", "Positions, newest first.",
                       {**_OBJ, "properties": {"status": {"type": "string", "enum": ["open", "closed", "all"]},
                                               "limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
                       ("GET", "/v1/positions"), True),
    "list_events": ("read", "What the relay reported (alerts, fills, problems), newest first.",
                    {**_OBJ, "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200},
                                            "kind": {"type": "string"}}},
                    ("GET", "/v1/events"), True),
    "list_signals": ("read", "The signals that came in and what became of them, newest first.",
                     {**_OBJ, "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
                     ("GET", "/v1/signals"), True),
    "setup_show": ("read", ("The settings as written on disk (deployment, sources, sinks), which variables are "
                           "set (never their values), the gates before live, and whether a restart is pending."),
                   {**_OBJ, "properties": {}}, ("GET", "/v1/setup"), True),
    "setup_catalog": ("read", "The exchanges the relay can use, their test environments and what each can do.",
                      {**_OBJ, "properties": {}}, ("GET", "/v1/setup/catalog"), True),
    "setup_write": ("setup", ("Write one settings file after the relay's own checks (the old version is kept). "
                             "A deployment keeps its paper or live state; this never switches to live. Numbers "
                             "in it are the owner's own. Takes effect after restart_relay."),
                    {**_OBJ, "required": ["kind", "name", "settings"],
                     "properties": {"kind": KIND, "name": NAME, "settings": {"type": "object"}}},
                    ("PUT", "/v1/setup/{kind}/{name}"), False),
    "setup_remove": ("setup", "Remove a source or sink file (its last version is kept).",
                     {**_OBJ, "required": ["kind", "name"],
                      "properties": {"kind": {"type": "string", "enum": ["source", "sink"]}, "name": NAME}},
                     ("DELETE", "/v1/setup/{kind}/{name}"), False),
    "restart_relay": ("setup", ("Restart the relay so it reads its settings again (orders in flight finish first). "
                               "Under Docker it comes back by itself within seconds."),
                      {**_OBJ, "properties": {}}, ("POST", "/v1/setup/restart"), False),
    "send_test_signal": ("setup", ("Send a paper signal through the whole relay (checks, rails, a simulated fill). "
                                  "It never reaches the exchange. Size and stop are the owner's."),
                         {"type": "object", "required": ["action", "symbol"],
                          "properties": {"action": {"type": "string"}, "symbol": {"type": "string"},
                                         "side": {"type": "string", "enum": ["long", "short"]},
                                         "size": {"type": "object"}, "stop": {"type": "object"}}},
                         ("POST", "/v1/setup/test-signal"), False),
    "venue_check": ("setup", ("Check the exchange connector on the exchange's demo or test account: a small "
                             "position, its stop set and moved, then closed. Never on a live account. Size and "
                             "stop distance are the owner's."),
                    {**_OBJ, "required": ["symbol", "notional", "stop_pct"],
                     "properties": {"symbol": {"type": "string"}, "notional": {"type": "number"},
                                    "stop_pct": {"type": "number"},
                                    "environment": {"type": "string", "enum": ["demo", "testnet"]}}},
                    ("POST", "/v1/setup/venue-check"), False),
}


def _rpc_error(id_, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}},
                        status_code=status)


def _result(id_, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


async def _caller(request: Request, oauth: OAuth) -> tuple[set[str] | None, str | None]:
    """(the scopes this request may use, or None when it is not signed in; the client address)."""
    guard = request.app.state.guard
    who = guard.client(request.client.host if request.client else None, request.headers)
    auth = request.headers.get("Authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return None, who
    cfg = request.app.state.runtime.cfg
    if cfg.api_key and hmac.compare_digest(token.encode(), cfg.api_key.encode()):
        guard.succeeded(who)
        return {"read", "setup"}, who
    if cfg.api_readonly_key and hmac.compare_digest(token.encode(), cfg.api_readonly_key.encode()):
        return {"read"}, who                       # lesser credentials never clear the count (Guard.succeeded)
    row = await oauth.lookup(request.app.state.runtime.db, token)
    if row is not None:
        return set(row.scope.split()), who
    if guard.failed(who):
        from relay.api.auth import _report_lockout
        await _report_lockout(request, who, guard)
    return None, who


def router(oauth: OAuth) -> APIRouter:
    r = APIRouter(include_in_schema=False)

    @r.get("/mcp")
    async def no_stream(request: Request):
        return Response(status_code=405, headers={"Allow": "POST"})       # no server-sent stream offered

    @r.post("/mcp")
    async def mcp(request: Request):
        rt = request.app.state.runtime
        if rt is None or not rt.cfg.mcp_enabled:
            return JSONResponse({"detail": "the MCP endpoint is off (RELAY_MCP_ENABLED)"}, status_code=404)
        guard = request.app.state.guard
        left = guard.locked_for(guard.client(request.client.host if request.client else None, request.headers))
        if left:
            return JSONResponse({"detail": "too many wrong tokens from this address; try again later"},
                                status_code=429, headers={"Retry-After": str(int(left) + 1)})
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != base_url(request):
            # A web page in the owner's browser must not reach the endpoint (MCP: validate Origin).
            return JSONResponse({"detail": "this origin may not use the MCP endpoint"}, status_code=403)
        scopes, _ = await _caller(request, oauth)
        if scopes is None:
            meta = f"{base_url(request)}/.well-known/oauth-protected-resource/mcp"
            return JSONResponse({"detail": "sign in first"}, status_code=401,
                                headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})
        try:
            msg = json.loads(await request.body())
        except ValueError:
            return _rpc_error(None, -32700, "not JSON", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600,
                              "one JSON-RPC 2.0 message per request", 400)
        if "id" not in msg:
            return Response(status_code=202)                                 # a notification
        id_, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            return _result(id_, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "quanti-relay", "version": __version__},
                                 "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": [
                {"name": name, "description": desc, "inputSchema": schema,
                 "annotations": {"readOnlyHint": ro, "destructiveHint": False, "openWorldHint": False}}
                for name, (scope, desc, schema, _, ro) in TOOLS.items() if scope in scopes]})
        if method == "tools/call":
            if not isinstance(params, dict):
                return _rpc_error(id_, -32602, "params must be an object")
            return _result(id_, await _call(request, scopes, params.get("name"), params.get("arguments") or {}))
        return _rpc_error(id_, -32601, f"unknown method {method}")

    return r


async def _call(request: Request, scopes: set[str], name: Any, args: Any) -> dict:
    """A tool's answer: the API route's JSON as text, isError when the route refused."""
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        return _tool_error(f"unknown tool {name!r}")
    scope, _, _, (method, path), _ = tool
    if scope not in scopes:
        return _tool_error(f"{name} needs the '{scope}' permission, which this connection was not given")
    if not isinstance(args, dict):
        return _tool_error("arguments must be an object")
    rt = request.app.state.runtime
    if name in LIVE_LOCKED and (not rt.is_dry_run() or setup.live_on_disk(rt.cfg.config_dir)):
        return _tool_error(f"{name} is not available while the relay is live (or its deployment file says "
                           f"live): the owner changes a live relay in the console")
    query, body = None, None
    if name == "setup_write" or name == "setup_remove":
        kind, item = args.get("kind"), args.get("name")
        # Checked here, not only in the schema: the two go into the route's path, and a name like
        # "../../positions/1/trailing" must never reach another route.
        kinds = ("deployment", "source", "sink") if name == "setup_write" else ("source", "sink")
        if kind not in kinds or not isinstance(item, str) or not setup.NAME.match(item):
            return _tool_error(f"kind is one of {', '.join(kinds)}; a name is 1-50 lowercase letters, digits, "
                               f"'-' or '_'")
        path = path.format(kind=kind, name=item)
        body = args.get("settings") if name == "setup_write" else None
    elif method == "GET":
        query = {k: v for k, v in args.items() if isinstance(v, str | int)}
    else:
        body = args
    cfg = request.app.state.runtime.cfg
    peer = request.client.host if request.client else "127.0.0.1"
    transport = httpx.ASGITransport(app=request.app, client=(peer, 0))
    headers = [("Authorization", f"Bearer {cfg.api_key}")] if cfg.api_key else []
    # the same client for the allowlist and lockout: every line of the header the proxy writes
    headers += [(cfg.client_ip_header, v) for v in request.headers.getlist(cfg.client_ip_header)]
    async with httpx.AsyncClient(transport=transport, base_url="http://relay", headers=headers) as http:
        r = await http.request(method, path, params=query, json=body)
    try:
        data = r.json()
    except ValueError:
        data = {"status": r.status_code}
    text = json.dumps(data, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": r.status_code >= 400,
            **({"structuredContent": data} if isinstance(data, dict) else {})}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}
