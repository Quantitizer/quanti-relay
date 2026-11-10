"""What stands in front of the API when the relay is reachable from the internet.

* The client's address: the direct peer, or behind a TLS proxy you run (`RELAY_TRUSTED_PROXIES`)
  the right-most entry of its header (`RELAY_CLIENT_IP_HEADER`) that is not one of your proxies. A
  header from anyone else is ignored, so a client cannot claim another address.
* Wrong keys: an address that sends `auth_max_failures` wrong API keys within
  `auth_failure_window_s` is refused (429) for `auth_lockout_s`, whatever key it sends meanwhile.
  IPv6 addresses count per /64, the block one subscriber usually holds.
* `RELAY_API_ALLOW_IPS` (optional): only these addresses or networks may use the API, the console
  and the API documentation. Signal intake keeps its own rules (each source's) and /health stays open.
* Request bodies above `max_body_bytes` are refused (413) before they are read, and one that takes
  longer than BODY_TIMEOUT_S to arrive is cut off (408): slow senders cannot hold the connections.
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import OrderedDict, deque

from starlette.responses import JSONResponse

# Addresses with recent wrong keys that are remembered; the oldest is forgotten first, so many
# addresses cannot fill the memory.
TRACKED_MAX = 10_000
BODY_TIMEOUT_S = 15.0


class GuardConfigError(ValueError):
    pass


def _networks(name: str, listed: str) -> tuple:
    try:
        return tuple(ipaddress.ip_network(x.strip(), strict=False) for x in listed.split(",") if x.strip())
    except ValueError as e:
        raise GuardConfigError(f"RELAY_{name.upper()}: {e}") from None


def _address(text: str | None):
    """An address, with an IPv4 address written as IPv6 (::ffff:a.b.c.d) read as the IPv4 one."""
    try:
        addr = ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None
    return getattr(addr, "ipv4_mapped", None) or addr


def _in(addr, networks: tuple) -> bool:
    return addr is not None and any(addr in net for net in networks)


class Guard:
    def __init__(self, settings):
        self.trusted_proxies = _networks("trusted_proxies", settings.trusted_proxies)
        self.client_ip_header = settings.client_ip_header.strip()
        if self.trusted_proxies and not self.client_ip_header:
            raise GuardConfigError("RELAY_CLIENT_IP_HEADER: with RELAY_TRUSTED_PROXIES, name the header "
                                   "your proxy puts the client's address in")
        self.allow = _networks("api_allow_ips", settings.api_allow_ips)
        for name in ("auth_max_failures", "auth_failure_window_s", "auth_lockout_s", "max_body_bytes"):
            if getattr(settings, name) < 0:
                raise GuardConfigError(f"RELAY_{name.upper()} cannot be negative")
        self.max_failures = settings.auth_max_failures
        self.window_s = settings.auth_failure_window_s
        self.lockout_s = settings.auth_lockout_s
        self.max_body_bytes = settings.max_body_bytes
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._locked: OrderedDict[str, float] = OrderedDict()   # bucket -> monotonic time the lockout ends

    def client(self, peer: str | None, headers) -> str | None:
        """The client's address as text (None = cannot be told)."""
        addr = _address(peer)
        if not _in(addr, self.trusted_proxies):
            return None if addr is None else str(addr)
        # Every line of the header, in order: a proxy may add its own line instead of extending one
        lines = headers.getlist(self.client_ip_header) if hasattr(headers, "getlist") else \
            [headers.get(self.client_ip_header) or ""]
        for entry in reversed(",".join(lines).split(",")):
            if not entry.strip():
                continue
            entry_addr = _address(entry)
            if entry_addr is None:
                return None                       # garbage after our proxy: cannot be told
            if not _in(entry_addr, self.trusted_proxies):
                return str(entry_addr)
        return str(addr)                          # only proxies: the request began at one of them

    def allowed(self, client: str | None) -> bool:
        return not self.allow or _in(_address(client), self.allow)

    @staticmethod
    def bucket(client: str | None) -> str:
        addr = _address(client)
        if addr is None:
            return "unknown"
        if addr.version == 6:
            return str(ipaddress.ip_network(f"{addr}/64", strict=False))
        return str(addr)

    def locked_for(self, client: str | None, now: float | None = None) -> float:
        """Seconds this client stays locked out (0 = not locked)."""
        b = self.bucket(client)
        until = self._locked.get(b)
        if until is None:
            return 0.0
        left = until - (time.monotonic() if now is None else now)
        if left <= 0:
            del self._locked[b]
            return 0.0
        return left

    def failed(self, client: str | None, now: float | None = None) -> bool:
        """Count a wrong key. True when this one locks the client out."""
        if not self.max_failures:
            return False
        now = time.monotonic() if now is None else now
        b = self.bucket(client)
        times = self._failures.pop(b, None) or deque()
        self._failures[b] = times                 # most recent last
        while times and times[0] <= now - self.window_s:
            times.popleft()
        times.append(now)
        while len(self._failures) > TRACKED_MAX:
            self._failures.popitem(last=False)
        if len(times) < self.max_failures:
            return False
        del self._failures[b]
        self._locked.pop(b, None)
        self._locked[b] = now + self.lockout_s
        while len(self._locked) > TRACKED_MAX:
            self._locked.popitem(last=False)          # the oldest lockout ends first anyway
        return True

    def succeeded(self, client: str | None) -> None:
        """The full API key was given: the count starts again. A lesser credential (the read-only
        key, a chat app's token) never clears it, or its holder could guess the full key forever."""
        self._failures.pop(self.bucket(client), None)


class BodyLimitMiddleware:
    """Refuses a request body larger than the limit (413), by its Content-Length or, for one sent
    in chunks, as soon as it grows past it. A plain ASGI middleware: the body is never held whole."""

    def __init__(self, app, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.limit:
            return await self.app(scope, receive, send)
        for key, value in scope.get("headers") or ():
            if key == b"content-length":
                try:
                    too_big = int(value) > self.limit
                except ValueError:
                    too_big = True
                if too_big:
                    return await self._refuse(scope, receive, send)
        seen = 0
        started = False
        deadline = time.monotonic() + BODY_TIMEOUT_S

        body_done = False

        async def counted_receive():
            nonlocal seen, body_done
            if body_done:                         # afterwards the app only listens for a disconnect
                return await receive()
            try:
                message = await asyncio.wait_for(receive(), max(deadline - time.monotonic(), 0.001))
            except TimeoutError:
                raise _TooSlow from None
            if message["type"] != "http.request" or not message.get("more_body"):
                body_done = True
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.limit:
                    raise _TooBig
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except* _TooBig:                          # except*: the auth middleware's task group wraps it
            if not started:
                await self._refuse(scope, receive, send)
        except* _TooSlow:
            if not started:
                await JSONResponse({"detail": f"the request body did not arrive within {BODY_TIMEOUT_S:.0f} s"},
                                   status_code=408)(scope, receive, send)

    async def _refuse(self, scope, receive, send):
        response = JSONResponse({"detail": f"a request body is at most {self.limit} bytes"}, status_code=413)
        await response(scope, receive, send)


class _TooBig(Exception):
    pass


class _TooSlow(Exception):
    pass
