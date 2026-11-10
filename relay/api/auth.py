"""Two-tier API-key authentication.

* `api_key` (RELAY_API_KEY)          — full access, including every endpoint that places or closes orders.
* `api_readonly_key` (RELAY_API_READONLY_KEY) — optional; accepted ONLY for GET/HEAD, any mutating
  method gets 403. A dashboard or monitor can hold a credential that CANNOT place an order or
  flatten the book even if it leaks.

The key is sent as `Authorization: Bearer <key>` or `X-API-Key: <key>`. Public: `/health` and the
API documentation page (`/docs`, `/openapi.json`; it describes the API, every call still needs the key).
With no key configured the API FAILS CLOSED (503) — unless env == "development", for local use.
In front of all that: the address allowlist and the wrong-key lockout (relay.api.guard).
"""
import hmac
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from relay.api.guard import Guard
from relay.config import settings as default_settings

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/docs/oauth2-redirect",
                # the console's page, script and style: no data, it reads the API with the user's key
                "/console", "/console/", "/console/app.js", "/console/style.css"}
# Open whatever RELAY_API_ALLOW_IPS says: monitors must reach it from anywhere.
ALWAYS_OPEN = {"/health"}
# Requests that prove themselves another way: a signal carries its source's HMAC signature
# (relay.signals.sources), checked by the endpoint itself. Reading signals still needs the key.
SELF_AUTHENTICATED = {("POST", "/v1/signals")}
SELF_AUTHENTICATED_SOURCE_PATH = re.compile(r"^/v1/sources/[^/]+/signals$")   # POST, the source named in the path
SELF_AUTHENTICATED_LIMITS_PATH = re.compile(r"^/v1/sources/[^/]+/limits$")    # GET, signed the same way

# The chat-app connector signs in its own way (relay.api.oauth, relay.api.mcp): these check their own
# tokens or the owner's key, after the address allowlist like everything else.
OWN_SIGN_IN = {"/mcp", "/oauth/register", "/oauth/authorize", "/oauth/token",
               "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
               "/.well-known/oauth-protected-resource/mcp"}

# Read-only methods a read-tier key may use. Everything that MUTATES state (open/close orders,
# flatten, sync, config changes) is POST/PUT/PATCH/DELETE and therefore requires the full key.
READ_METHODS = {"GET", "HEAD"}
SAFETY_ROUTES = {"/v1/emergency-stop", "/v1/close-all"}


def _presented_key(request) -> str:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key") or ""


class APIKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings=None, guard: Guard | None = None):
        super().__init__(app)
        self.settings = settings or default_settings
        self.guard = guard or Guard(self.settings)

    async def dispatch(self, request, call_next):
        path = request.url.path
        if (request.method, path) in SELF_AUTHENTICATED or (
                request.method == "POST" and SELF_AUTHENTICATED_SOURCE_PATH.match(path)) or (
                request.method == "GET" and SELF_AUTHENTICATED_LIMITS_PATH.match(path)):
            return await call_next(request)               # signals: each source's own rules
        if path in ALWAYS_OPEN:
            return await call_next(request)
        client = self.guard.client(request.client.host if request.client else None, request.headers)
        if not self.guard.allowed(client):
            return JSONResponse(status_code=403, content={"detail": "this address may not use the API "
                                                                    "(RELAY_API_ALLOW_IPS)"})
        if path in PUBLIC_PATHS or path in OWN_SIGN_IN:
            return await call_next(request)

        configured = self.settings.api_key
        if not configured:
            # No key configured. In development auth is OFF (local convenience). In ANY other env
            # this is a misconfiguration on a service that places orders — FAIL CLOSED rather than
            # exposing every endpoint because one environment variable is missing.
            if self.settings.env == "development":
                return await call_next(request)
            return JSONResponse(
                status_code=503,
                content={"detail": "API key not configured. Set RELAY_API_KEY "
                                   "(or RELAY_ENV=development for local use)."},
            )

        # A locked-out address is refused before its key is even compared: guessing goes nowhere.
        # Except the full key on the safety routes: a stranger behind the same address (a mobile
        # carrier's shared one) must not keep the owner from stopping. The key is long and random,
        # so letting it through there is no help to anyone guessing it.
        left = self.guard.locked_for(client)
        if left and request.method == "POST" and path in SAFETY_ROUTES and hmac.compare_digest(
                _presented_key(request).encode(), configured.encode()):
            return await call_next(request)
        if left:
            return JSONResponse(status_code=429, headers={"Retry-After": str(int(left) + 1)},
                                content={"detail": "too many wrong API keys from this address; try again later"})

        api_key = _presented_key(request)
        # Constant-time compare (avoids a timing side channel on the key).
        if hmac.compare_digest(api_key.encode(), configured.encode()):
            self.guard.succeeded(client)
            return await call_next(request)

        # Read-only tier: valid ONLY for safe methods. A leaked read key can never mutate state.
        readonly = self.settings.api_readonly_key
        if readonly and hmac.compare_digest(api_key.encode(), readonly.encode()):
            if request.method in READ_METHODS:
                return await call_next(request)
            return JSONResponse(
                status_code=403,
                content={"detail": "Read-only API key cannot perform write operations."},
            )

        if self.guard.failed(client):
            await _report_lockout(request, client, self.guard)
        return JSONResponse(status_code=401, content={"detail": "Invalid API key"})


async def _report_lockout(request, client, guard: Guard) -> None:
    rt = getattr(request.app.state, "runtime", None)
    events = getattr(rt, "events", None)
    message = (f"{guard.max_failures} wrong API keys from {guard.bucket(client)} within "
               f"{guard.window_s} s: refused for {guard.lockout_s} s")
    if events is None:
        from loguru import logger
        logger.warning(f"[Auth] {message}")
        return
    await events.record("auth.locked", "warning", message, {"address": guard.bucket(client)})
