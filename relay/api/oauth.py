"""The sign-in a chat app uses to connect to this relay's MCP endpoint (relay.api.mcp): OAuth 2.1
with PKCE, as the MCP specification asks for.

  /.well-known/oauth-protected-resource[/mcp]   which server signs in for /mcp
  /.well-known/oauth-authorization-server       this relay's endpoints
  POST /oauth/register                          a chat app registers itself (public client, PKCE)
  GET  /oauth/authorize                         the owner's page: which app, what it may do, the API key
  POST /oauth/authorize                         the owner approves with the API key -> a one-time code
  POST /oauth/token                             code (+ verifier) -> access and refresh token

Only the owner can approve, by typing the relay's API key on the relay's own page; wrong keys count
towards the address lockout (relay.api.guard). The page names the address the app will be sent
back to, so a look-alike app cannot pass as another. Tokens are random, kept only as their SHA-256
in the database, and can be revoked (GET/DELETE /v1/oauth/clients, with the API key). An access
token lasts ACCESS_TTL; the refresh token is replaced at every use.

Scopes: `read` (status, positions, events, signals, the setup as written) and `setup` (write config
files, restart, paper test signals, the venue check on a test account). Going live, the emergency
stop and closing positions are not offered here at all.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, update

from relay.db.models import OAuthClient, OAuthToken

SCOPES = {"read": "see the relay's status, positions, events, signals and settings",
          "setup": "change the settings (never live), restart the relay, send paper test signals and run the "
                   "venue check on a test account"}
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL_S = 300
CLIENTS_MAX = 20                    # approved apps; more are refused until one is removed
UNAPPROVED_TTL = timedelta(hours=1)  # a registration not approved by then is let go
UNAPPROVED_MAX = 50                 # registrations waiting for approval; the oldest goes first
REGISTER_PER_ADDRESS = 10           # registrations one address may make per hour
CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {back}; base-uri 'none'; "
       "frame-ancestors 'none'")
PAGE_HEADERS = {
    "Content-Security-Policy": CSP.format(back=""),
    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def base_url(request: Request) -> str:
    """The relay's own address as the app sees it: RELAY_HOST behind the TLS proxy, else the request's."""
    host = request.app.state.runtime.cfg.public_host if request.app.state.runtime else ""
    return f"https://{host}" if host else str(request.base_url).rstrip("/")


def _redirect_ok(uri: str) -> bool:
    """https anywhere, or http on this machine only (a desktop app's local callback)."""
    if any(ch.isspace() or not ch.isprintable() for ch in uri):
        return False                                 # urlsplit would drop some of them silently
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment or not u.hostname or u.username is not None or u.password is not None:
        return False
    try:
        _ = u.port                                   # a port that is not a number raises
    except ValueError:
        return False
    if not (_HOST.match(u.hostname) or u.hostname == "::1"):
        return False                                 # a plain host: it goes into the page's CSP as well
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))


def _origin(uri: str) -> str:
    u = urlsplit(uri)
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")


async def _form(request: Request) -> dict[str, str]:
    """A form post (application/x-www-form-urlencoded, as OAuth sends it), read with the standard
    library: no multipart, no files. A repeated field keeps its first value."""
    if not (request.headers.get("content-type") or "").startswith("application/x-www-form-urlencoded"):
        return {}
    out: dict[str, str] = {}
    for k, v in parse_qsl((await request.body()).decode("utf-8", "replace"), keep_blank_values=True):
        out.setdefault(k, v)
    return out


def _error(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status,
                        headers={"Cache-Control": "no-store"})


class OAuth:
    def __init__(self):
        self.codes: dict[str, dict[str, Any]] = {}       # one-time codes, a few minutes, in memory
        self.registrations: dict[str, deque[float]] = {}  # address -> when it registered (the last hour)

    def may_register(self, bucket: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        for b in [b for b, t in self.registrations.items() if not t or t[-1] <= now - 3600]:
            del self.registrations[b]
        times = self.registrations.setdefault(bucket, deque())
        while times and times[0] <= now - 3600:
            times.popleft()
        if len(times) >= REGISTER_PER_ADDRESS:
            return False
        times.append(now)
        return True

    # ---- tokens --------------------------------------------------------------------------------
    async def issue(self, db, client_id: str, scope: str) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        async with db.session() as s:
            s.add(OAuthToken(token_hash=token_hash(access), kind="access", client_id=client_id, scope=scope,
                             created_at=now, expires_at=now + ACCESS_TTL))
            s.add(OAuthToken(token_hash=token_hash(refresh), kind="refresh", client_id=client_id, scope=scope,
                             created_at=now, expires_at=now + REFRESH_TTL))
            await s.commit()
        return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()),
                "refresh_token": refresh, "scope": scope}

    async def lookup(self, db, token: str, kind: str = "access") -> OAuthToken | None:
        """The live token row for this token, or None (unknown, expired, revoked, its app removed)."""
        async with db.session() as s:
            row = await s.get(OAuthToken, token_hash(token))
            if row is None or row.kind != kind or row.revoked:
                return None
            expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                return None
            client = await s.get(OAuthClient, row.client_id)
            if client is None or client.revoked_at is not None:
                return None
            return row

    async def revoke(self, db, token: str) -> None:
        async with db.session() as s:
            row = await s.get(OAuthToken, token_hash(token))
            if row is not None:
                row.revoked = True
                await s.commit()

    async def use_refresh(self, db, token: str) -> OAuthToken | None:
        """Spend a refresh token: its row when this call was the one that spent it, else None. Two
        calls at once cannot both win. A token spent before means a copy is in someone else's
        hands: every token of that app is revoked, and the app must be approved again."""
        row = await self.lookup(db, token, "refresh")
        async with db.session() as s:
            if row is None:
                old = await s.get(OAuthToken, token_hash(token))
                if old is not None and old.kind == "refresh" and old.revoked:
                    await s.execute(update(OAuthToken).where(OAuthToken.client_id == old.client_id)
                                    .values(revoked=True))
                    await s.commit()
                return None
            done = await s.execute(update(OAuthToken).where(OAuthToken.token_hash == row.token_hash,
                                                            OAuthToken.revoked.is_(False)).values(revoked=True))
            await s.commit()
            return row if done.rowcount == 1 else None

    # ---- the one-time codes --------------------------------------------------------------------
    def new_code(self, **grant) -> str:
        now = time.monotonic()
        for c in [c for c, g in self.codes.items() if g["expires"] <= now]:
            del self.codes[c]
        code = secrets.token_urlsafe(32)
        self.codes[code] = {**grant, "expires": now + CODE_TTL_S}
        return code

    def take_code(self, code: str) -> dict[str, Any] | None:
        grant = self.codes.pop(code, None)                # one use, whatever happens next
        if grant is None or grant["expires"] <= time.monotonic():
            return None
        return grant


def _verifier_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


def router(oauth: OAuth) -> APIRouter:
    r = APIRouter(include_in_schema=False)

    def enabled(request: Request) -> bool:
        rt = request.app.state.runtime
        return bool(rt is not None and rt.cfg.mcp_enabled)

    def off() -> JSONResponse:
        return JSONResponse({"detail": "the MCP endpoint is off (RELAY_MCP_ENABLED)"}, status_code=404)

    @r.get("/.well-known/oauth-protected-resource")
    @r.get("/.well-known/oauth-protected-resource/mcp")
    async def protected_resource(request: Request):
        if not enabled(request):
            return off()
        base = base_url(request)
        return {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": list(SCOPES),
                "bearer_methods_supported": ["header"], "resource_name": "Quanti Relay"}

    @r.get("/.well-known/oauth-authorization-server")
    async def authorization_server(request: Request):
        if not enabled(request):
            return off()
        base = base_url(request)
        return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token", "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": list(SCOPES)}

    @r.post("/oauth/register")
    async def register(request: Request):
        if not enabled(request):
            return off()
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(400, "invalid_client_metadata", "a JSON object")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 5 or not all(
                isinstance(u, str) and len(u) <= 500 and _redirect_ok(u) for u in uris):
            return _error(400, "invalid_redirect_uri", "1-5 redirect_uris, https (or http on localhost)")
        name = str(body.get("client_name") or "an app")[:80]
        guard = request.app.state.guard
        who = guard.client(request.client.host if request.client else None, request.headers)
        if not oauth.may_register(guard.bucket(who)):
            return _error(429, "invalid_client_metadata", "too many registrations from this address; try later")
        db = request.app.state.runtime.db
        async with db.session() as s:
            # Registrations nobody approved take no approved app's place: they are let go after an
            # hour, and beyond UNAPPROVED_MAX the oldest goes. Strangers cannot keep the owner out.
            approved = select(OAuthToken.client_id)
            waiting = (await s.execute(select(OAuthClient).where(
                OAuthClient.revoked_at.is_(None), OAuthClient.client_id.not_in(approved))
                .order_by(OAuthClient.created_at))).scalars().all()
            cutoff = datetime.now(UTC) - UNAPPROVED_TTL
            for i, c in enumerate(waiting):
                created = c.created_at if c.created_at.tzinfo else c.created_at.replace(tzinfo=UTC)
                if created < cutoff or i <= len(waiting) - UNAPPROVED_MAX:
                    c.revoked_at = datetime.now(UTC)
            await s.flush()
            active = (await s.execute(select(OAuthClient).where(
                OAuthClient.revoked_at.is_(None), OAuthClient.client_id.in_(approved)))).scalars().all()
            if len(active) >= CLIENTS_MAX:
                return _error(400, "invalid_client_metadata", f"{CLIENTS_MAX} apps are connected already: remove "
                                                              f"one first (DELETE /v1/oauth/clients/<id>)")
            client = OAuthClient(client_id="rc_" + secrets.token_urlsafe(18), name=name, redirect_uris=uris,
                                 created_at=datetime.now(UTC))
            s.add(client)
            await s.commit()
        return JSONResponse({"client_id": client.client_id, "client_name": name, "redirect_uris": uris,
                             "token_endpoint_auth_method": "none",
                             "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]},
                            status_code=201)

    async def _client(request: Request, client_id: str) -> OAuthClient | None:
        async with request.app.state.runtime.db.session() as s:
            c = await s.get(OAuthClient, client_id or "")
            return c if c is not None and c.revoked_at is None else None

    def _scope(asked: str | None) -> str | None:
        wanted = (asked or "read setup").split()
        return " ".join(s for s in SCOPES if s in wanted) if set(wanted) <= set(SCOPES) else None

    async def _check(request: Request, p) -> tuple[OAuthClient | None, str | None, str | None]:
        """(client, scope, problem) for an authorize request's parameters."""
        if p.get("response_type") != "code":
            return None, None, "response_type must be code"
        client = await _client(request, p.get("client_id"))
        if client is None:
            return None, None, "unknown app (client_id): register it first"
        if p.get("redirect_uri") not in client.redirect_uris:
            return None, None, "the redirect_uri is not one this app registered"
        if p.get("code_challenge_method") != "S256" or not 43 <= len(p.get("code_challenge") or "") <= 128:
            return None, None, "PKCE with S256 is required"
        scope = _scope(p.get("scope"))
        if scope is None:
            return None, None, f"unknown scope: the scopes are {', '.join(SCOPES)}"
        return client, scope, None

    def _page(body: str, status: int = 200, back: str = "") -> HTMLResponse:
        return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect an app</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;color:#1d2330}}
code{{background:#eef1f5;padding:0 .25rem;border-radius:4px}}input[type=password]{{width:100%;padding:.5rem;font:inherit}}
button{{padding:.5rem 1rem;font:inherit;margin-top:1rem}}.warn{{color:#8a2c0d}}</style></head><body>{body}</body></html>""",
                            status_code=status,
                            headers={**PAGE_HEADERS, "Content-Security-Policy": CSP.format(back=back)})

    @r.get("/oauth/authorize")
    async def authorize_page(request: Request):
        if not enabled(request):
            return off()
        p = request.query_params
        client, scope, problem = await _check(request, p)
        if problem:
            return _page(f"<h1>Cannot connect</h1><p>{html.escape(problem)}</p>", 400)
        back = urlsplit(p["redirect_uri"]).hostname
        allowed = f"<li>{html.escape(SCOPES['read'])}</li>" if "read" in scope.split() else ""
        if "setup" in scope.split():
            allowed += (f'<li><label><input type="checkbox" name="allow_setup" value="1" checked> '
                        f'{html.escape(SCOPES["setup"])}</label></li>')
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(p.get(k) or "", quote=True)}">'
                         for k in ("client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state",
                                   "response_type", "resource"))
        return _page(f"""<h1>Connect {html.escape(client.name)} to this relay?</h1>
<p>After you approve, you are sent back to <code>{html.escape(back)}</code>. Approve only if you started this
from the app you trust, and that is its address.</p>
<form method="post" action="/oauth/authorize">{hidden}
<p>It may:</p><ul>{allowed}</ul>
<p>It can never switch to live trading, use the emergency stop or close positions, and it changes
nothing while the relay runs live.</p>
<input type="hidden" name="scope" value="{html.escape(scope, quote=True)}">
<label>The relay's API key (only you have it)<br><input type="password" name="api_key" autocomplete="off" required></label><br>
<button type="submit">Approve</button></form>
<p class="warn">Wrong keys lock this address out for a while.</p>""", back=_origin(p["redirect_uri"]))

    @r.post("/oauth/authorize")
    async def authorize(request: Request):
        if not enabled(request):
            return off()
        p = await _form(request)
        client, scope, problem = await _check(request, p)
        if problem:
            return _page(f"<h1>Cannot connect</h1><p>{html.escape(problem)}</p>", 400)
        guard = request.app.state.guard
        who = guard.client(request.client.host if request.client else None, request.headers)
        left = guard.locked_for(who)
        if left:
            return _page("<h1>Too many wrong keys</h1><p>Try again later.</p>", 429)
        configured = request.app.state.runtime.cfg.api_key
        if not configured or not hmac.compare_digest(p.get("api_key", "").encode(), configured.encode()):
            if guard.failed(who):
                from relay.api.auth import _report_lockout
                await _report_lockout(request, who, guard)
            return _page("<h1>Not approved</h1><p>That is not the relay's API key. Go back and try again.</p>", 401)
        guard.succeeded(who)
        if "setup" in scope.split() and not p.get("allow_setup"):
            scope = " ".join(s for s in scope.split() if s != "setup") or None     # the owner said read only
        if scope is None:
            return _page("<h1>Nothing approved</h1><p>The app asked for nothing else.</p>", 400)
        code = oauth.new_code(client_id=client.client_id, redirect_uri=p["redirect_uri"], scope=scope,
                              challenge=p["code_challenge"])
        query = {"code": code, **({"state": p["state"]} if p.get("state") else {}), "iss": base_url(request)}
        sep = "&" if "?" in p["redirect_uri"] else "?"
        return RedirectResponse(p["redirect_uri"] + sep + urlencode(query), status_code=302)

    @r.post("/oauth/token")
    async def token(request: Request):
        if not enabled(request):
            return off()
        form = await _form(request)
        db = request.app.state.runtime.db
        grant = form.get("grant_type")
        if grant == "authorization_code":
            g = oauth.take_code(form.get("code", ""))
            if (g is None or g["client_id"] != form.get("client_id") or g["redirect_uri"] != form.get("redirect_uri")
                    or not _verifier_ok(form.get("code_verifier", ""), g["challenge"])):
                return _error(400, "invalid_grant", "the code is unknown, used, expired or does not match")
            if await _client(request, g["client_id"]) is None:
                return _error(400, "invalid_grant", "the app was removed")
            return JSONResponse(await oauth.issue(db, g["client_id"], g["scope"]), headers={"Cache-Control": "no-store"})
        if grant == "refresh_token":
            old = form.get("refresh_token", "")
            row = await oauth.lookup(db, old, "refresh")
            if row is not None and form.get("client_id") and form["client_id"] != row.client_id:
                return _error(400, "invalid_grant", "the refresh token is unknown, used or expired")
            row = await oauth.use_refresh(db, old)       # used once: a stolen copy is worthless afterwards
            if row is None:
                return _error(400, "invalid_grant", "the refresh token is unknown, used or expired")
            return JSONResponse(await oauth.issue(db, row.client_id, row.scope), headers={"Cache-Control": "no-store"})
        return _error(400, "unsupported_grant_type", "authorization_code or refresh_token")

    # ---- the owner's view (behind the API key, like every /v1 route) ------------------------------
    @r.get("/v1/oauth/clients")
    async def clients(request: Request):
        async with request.app.state.runtime.db.session() as s:
            rows = (await s.execute(select(OAuthClient).where(OAuthClient.revoked_at.is_(None))
                                    .order_by(OAuthClient.created_at))).scalars().all()
        return [{"client_id": c.client_id, "name": c.name, "redirect_uris": c.redirect_uris,
                 "created_at": c.created_at.isoformat()} for c in rows]

    @r.delete("/v1/oauth/clients/{client_id}")
    async def remove_client(request: Request, client_id: str):
        async with request.app.state.runtime.db.session() as s:
            c = await s.get(OAuthClient, client_id)
            if c is None or c.revoked_at is not None:
                return JSONResponse({"detail": "no such app"}, status_code=404)
            c.revoked_at = datetime.now(UTC)             # its tokens stop working at once (lookup)
            await s.commit()
        return {"removed": client_id}

    return r
