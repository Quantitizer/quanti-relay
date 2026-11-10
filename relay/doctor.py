"""`relay doctor`: everything a setup needs, checked in one go, each with what to do about it.

Each check is {check, status, detail, fix}: status ok, warn (works, but you should know) or fail
(the relay will not start, or will not do what it is set up for). `relay doctor --json` prints the
same as one JSON object, for an AI agent to read; the exit code is 1 when anything fails.

It reads the settings and files the relay would start with, and with `--online` also asks the
exchange what the key may do (relay.venue.keycheck). `--url` checks a running relay's /health,
and `--public-url` the address a sender such as TradingView would use. It never changes anything.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MIN_KEY_LENGTH = 32


def _c(check: str, status: str, detail: str, fix: str = "") -> dict[str, Any]:
    return {"check": check, "status": status, "detail": detail, "fix": fix}


def offline_checks(settings, environ=None) -> tuple[list[dict[str, Any]], Any]:
    """(checks, the deployment or None) from the settings and the files alone."""
    from relay import setup
    from relay.api.guard import Guard, GuardConfigError
    from relay.deployment_config import DeploymentConfigError, load_deployment
    environ = os.environ if environ is None else environ
    out = []

    key = settings.api_key
    if not key:
        out.append(_c("api_key", "fail", "RELAY_API_KEY is not set: the relay refuses every request",
                      "relay secret generate RELAY_API_KEY"))
    elif len(key) < MIN_KEY_LENGTH:
        out.append(_c("api_key", "fail", f"RELAY_API_KEY is {len(key)} characters: on a server the relay asks for "
                      f"at least {MIN_KEY_LENGTH}", "relay secret set RELAY_API_KEY (or generate a new one)"))
    else:
        out.append(_c("api_key", "ok", "RELAY_API_KEY is set"))
    ro = settings.api_readonly_key
    if ro and len(ro) < MIN_KEY_LENGTH:
        out.append(_c("api_readonly_key", "fail", f"RELAY_API_READONLY_KEY is shorter than {MIN_KEY_LENGTH} characters",
                      "relay secret set RELAY_API_READONLY_KEY"))

    try:
        Guard(settings)
        out.append(_c("network_settings", "ok", "the proxy and allowlist settings read correctly"))
    except GuardConfigError as e:
        out.append(_c("network_settings", "fail", str(e), "correct it in .env"))

    config_dir = Path(settings.config_dir)
    if not config_dir.is_dir():
        out.append(_c("config_folder", "fail", f"the config folder {config_dir} does not exist",
                      f"mkdir -p {config_dir}/deployments {config_dir}/sources {config_dir}/sinks"))
    elif not setup.writable(config_dir):
        out.append(_c("config_folder", "warn", f"{config_dir} cannot be written: the console and `relay setup` "
                      "cannot save settings", "on Linux: sudo chown -R 10001:10001 config"))
    else:
        out.append(_c("config_folder", "ok", f"{config_dir} is there and writable"))

    dep = None
    try:
        dep = load_deployment(config_dir)
        if dep is None:
            out.append(_c("deployment", "warn", "no deployment file: the relay runs the built-in default (native "
                          "Bybit, settings from the environment)", "relay setup write deployment <name> --file -"))
        else:
            v = dep.venue
            out.append(_c("deployment", "ok", f"{dep.name}: {v.exchange} {v.market}, environment {v.environment}"))
    except DeploymentConfigError as e:
        out.append(_c("deployment", "fail", str(e), "correct the deployment file (relay setup show)"))

    paper = bool(settings.dry_run) or (dep is not None and dep.dry_run)     # the relay's own rule (Runtime.is_dry_run)
    out.append(_c("mode", "ok" if paper else "warn",
                  "paper (dry-run): no order reaches the exchange" if paper else
                  "LIVE: orders reach the exchange (the owner switched this on)"))

    missing = [k for k in setup.key_status(dep, environ) if not k["set"]]
    if missing:
        names = ", ".join(k["variable"] for k in missing)
        out.append(_c("exchange_keys", "warn" if paper else "fail",
                      f"not set: {names}" + (" (paper runs on public prices without them)" if paper else ""),
                      " ; ".join(f"relay secret set {k['variable']}" for k in missing)))
    elif dep is not None and not dep.venue.native and not setup.key_status(dep, environ):
        out.append(_c("exchange_keys", "warn", f"{dep.venue.exchange} is a venue plugin: it reads its own variables"))
    else:
        out.append(_c("exchange_keys", "ok", "the exchange's keys are set"))

    from relay.signals.sources import load_sources
    from relay.sinks import load_sinks
    for what, load in (("sources", load_sources), ("sinks", load_sinks)):
        try:
            loaded = load(config_dir, environ=environ)
            n = len(loaded)
            out.append(_c(what, "ok" if n else "warn",
                          f"{n} {what[:-1]}{'s' if n != 1 else ''} ready" if n else
                          ("no sources: no one can send signals yet" if what == "sources" else
                           "no notifications: you will only see events in the console"),
                          "" if n else f"relay setup write {what[:-1]} <name> --file -"))
        except Exception as e:
            out.append(_c(what, "fail", str(e), "correct the file, or set the variable it names (relay secret set ...)"))

    if settings.heartbeat_url:
        out.append(_c("heartbeat", "ok", "an outside monitor is called while the relay is healthy"))
    else:
        out.append(_c("heartbeat", "warn", "no outside monitor: nobody is told when the relay or its server is down",
                      "set RELAY_HEARTBEAT_URL to a heartbeat monitor of your choice"))
    if settings.mcp_enabled:
        out.append(_c("chat_connector", "ok", "the MCP endpoint for chat apps is on: https://<address>/mcp "
                      "(connected apps: GET /v1/oauth/clients)"))
    out.append(_database(settings.database_url))
    return out, dep


def _database(url: str) -> dict[str, Any]:
    from relay.db.backup import current_revision, sqlite_path
    path = sqlite_path(url)
    if path is None:
        return _c("database", "ok" if not url.startswith("sqlite") else "warn",
                  "Postgres: back it up with pg_dump" if not url.startswith("sqlite") else "an in-memory database")
    if not path.exists():
        return _c("database", "ok", f"{path} is created at the first start")
    try:
        scripts = _scripts()
        head = scripts.get_current_head()
        rev = current_revision(path)
    except Exception as e:
        return _c("database", "fail", f"{path} cannot be read: {type(e).__name__}", "check the file and the disk")
    if rev == head:
        return _c("database", "ok", f"{path} is at the latest schema")
    if rev is None:
        if path.stat().st_size == 0:
            return _c("database", "ok", f"{path} is set up at the first start")
        return _c("database", "warn", f"{path} has no schema version: the relay will try to set it up, without "
                  "a copy first", "relay db backup")
    try:
        known = scripts.get_revision(rev) is not None
    except Exception:
        known = False
    if not known:
        return _c("database", "fail", f"{path} is at schema {rev}, which this version of the relay does not know "
                  "(a newer version wrote it): it will not start", "update the relay to the version that wrote it, "
                  "or restore a copy from backups/")
    return _c("database", "ok", f"{path} is upgraded at the next start (a copy is made first)")


def _scripts():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "relay" / "db" / "migrations"))
    return ScriptDirectory.from_config(cfg)


async def key_checks(dep, settings, server_ip: str | None) -> list[dict[str, Any]]:
    """Ask the exchange what the key may do (only with the keys set)."""
    from relay.venue.keycheck import check_key
    if dep is None or dep.venue.native:
        from relay.exchange.bybit.rest_client import BybitRestClient
        if not (os.environ.get("BYBIT_API_KEY") and os.environ.get("BYBIT_API_SECRET")):
            return []
        # Bybit's host comes from the environment settings (Runtime.venue), not the file
        env = "demo" if settings.bybit_demo else "testnet" if settings.testnet else "live"
        client = BybitRestClient(testnet=env == "testnet", demo=env == "demo")
        try:
            k = await check_key(client, "perpetual")
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await close()
    else:
        from relay.venue.factory import VenueConfigError, build_ccxt_venue, close_client
        v = dep.venue
        try:
            client = build_ccxt_venue(v.exchange, v.environment, paper=False, market=v.market)
        except VenueConfigError:
            return []                                    # keys not set: reported above
        except Exception as e:
            return [_c("key_readable", "fail", f"the exchange connector could not be built: {type(e).__name__}: {e}"[:300],
                       "relay check-config")]
        try:
            k = await check_key(client, v.market)
        finally:
            await close_client(client)
    return [{**c, "fix": ""} for c in k.verdict(server_ip)]


async def http_check(check: str, url: str) -> dict[str, Any]:
    import httpx
    target = url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
            r = await http.get(target)
    except httpx.HTTPError as e:
        return _c(check, "fail", f"{target} did not answer ({type(e).__name__})",
                  "is the relay running, and the port or the TLS proxy open?")
    try:
        body = r.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    status = body.get("status")
    if body.get("service") != "quanti-relay":
        return _c(check, "fail", f"{target} answered {r.status_code}, but not as a Quanti Relay",
                  "is this the relay's address? Another service may answer there")
    if r.status_code == 200 and status == "ok":
        return _c(check, "ok", f"{target}: ok")
    return _c(check, "fail", f"{target} answered {r.status_code} ({status or 'no status'})",
              "relay doctor on the server; the console's Status tab")


def render(checks: list[dict[str, Any]]) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    lines = []
    for c in checks:
        lines.append(f"{mark[c['status']]} {c['check']}: {c['detail']}")
        if c.get("fix") and c["status"] != "ok":
            lines.append(f"     fix: {c['fix']}")
    fails = sum(c["status"] == "fail" for c in checks)
    warns = sum(c["status"] == "warn" for c in checks)
    lines.append(f"{'all good' if not fails else f'{fails} to fix'}" + (f", {warns} to know about" if warns else ""))
    return "\n".join(lines)
