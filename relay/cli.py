"""`relay` command line.

  relay serve [--host 127.0.0.1] [--port 8080]   run the API and background jobs
  relay db upgrade                              apply database migrations (SQLite: a copy first)
  relay db backup                               copy the SQLite database to backups/ beside it
  relay check-config                            read the deployment and source files, report problems
  relay test-signal --source NAME ...           send a signed DRY-RUN signal to a running relay
  relay doctor [--json] [--online]              check the whole setup and say what to fix
  relay setup show|catalog|write|remove         the config files, with JSON answers (never live)
  relay secret set|generate|reveal|list NAME    secrets at the terminal, never shown to an agent
  relay version
"""

import argparse
import os
import sys
from pathlib import Path

from relay import __version__

# API keys on a reachable address are at least this long (the same as source secrets).
MIN_KEY_LENGTH = 32


def _check_venue(dep, paper: bool) -> bool:
    """check-config for a CCXT exchange or a venue plugin: its environment, keys, tier and what it
    cannot do."""
    from relay.venue.factory import (
        VenueConfigError,
        build_ccxt_venue,
        client_problems,
        key_env_names,
    )
    exchange, environment, market = dep.venue.exchange, dep.venue.environment, dep.venue.market
    where = "MAINNET (real money)" if environment == "live" else environment
    print(f"mode: {'paper (dry-run): no order reaches the venue' if paper else 'LIVE'} - venue: {exchange}"
          f"{' spot (long only, no leverage)' if market == 'spot' else ''}, environment: {where}")
    try:
        venue = build_ccxt_venue(exchange, environment, paper=paper, market=market)
    except VenueConfigError as e:
        print(f"venue: PROBLEM - {e}")
        return False
    p = getattr(venue, "profile", None)
    if p is None:
        print(f"venue: {exchange} - a venue plugin (venue id {venue.venue_id}); its keys are its own to read")
    else:
        keys = key_env_names(p)
        unset = [v for v in keys.values() if not os.environ.get(v)]
        print(f"venue: {p.title} - {'verified' if p.verified else 'community connector (not yet verified)'}; "
              f"keys {', '.join(keys.values())}" + (f" ({', '.join(unset)} not set: paper only)" if unset else " set"))
    refusals, warnings = client_problems(venue, dep.execution_settings())
    for w in warnings:
        print(f"  note: {w}")
    for r in refusals:
        print(f"venue: PROBLEM - {r}")
    return not refusals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay", description="Quanti Relay")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the API and background jobs")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    serve.add_argument("--port", type=int, default=8080)

    db = sub.add_parser("db", help="database commands")
    db.add_argument("action", choices=["upgrade", "backup"])

    sub.add_parser("check-config", help="read the deployment and source files and report problems")

    ts = sub.add_parser("test-signal", help="send a signed dry-run signal to a running relay")
    ts.add_argument("--source", required=True, help="the source file's name (its secret is read from the environment)")
    ts.add_argument("--action", default="open", choices=["open", "close", "reduce", "flip", "close_all", "move_stop"])
    ts.add_argument("--symbol", help="e.g. BTCUSDT or BTC/USDT:USDT")
    ts.add_argument("--side", choices=["long", "short"])
    size = ts.add_mutually_exclusive_group()
    size.add_argument("--notional", type=float, help="open/flip: the size in USD")
    size.add_argument("--close-pct", type=float, help="reduce: %% of the position to close")
    stop = ts.add_mutually_exclusive_group()
    stop.add_argument("--stop-pct", type=float, help="open/flip: the stop, %% from the entry")
    stop.add_argument("--stop-price", type=float, help="open/flip/move_stop: the stop price")
    ts.add_argument("--weights", help="a TARGET book instead of an action, e.g. BTCUSDT=<WEIGHT>,ETHUSDT=-<WEIGHT> "
                                      "(signed fractions of equity; --stop-pct applies to every leg)")
    ts.add_argument("--gross-leverage", type=float, help="target book: multiplier on the weights")
    ts.add_argument("--deployment", help="default: the deployment the relay runs")
    ts.add_argument("--url", default="http://127.0.0.1:8080", help="the relay (default: %(default)s)")
    ts.add_argument("--print-only", action="store_true", help="print the signed request instead of sending it")

    vv = sub.add_parser("verify-venue", help="run the connector through a test trade on the exchange's demo or "
                                              "test account (never live) and report each step")
    vv.add_argument("--symbol", required=True, help="e.g. BTCUSDT (it must hold no position on that account)")
    vv.add_argument("--notional", type=float, required=True, help="the test position's size in USDT")
    vv.add_argument("--stop-pct", type=float, required=True, help="the test stop's distance, %% from the price")
    vv.add_argument("--confirm", action="store_true", help="place the orders (without it: only say what would run)")

    doc = sub.add_parser("doctor", help="check the whole setup and say what to fix (changes nothing)")
    doc.add_argument("--json", action="store_true", help="one JSON object, for an AI agent")
    doc.add_argument("--online", action="store_true", help="also ask the exchange what the key may do")
    doc.add_argument("--server-ip", help="this server's public address, to check the key is locked to it "
                                         "(default: RELAY_PUBLIC_IP, which deploy/install.sh sets)")
    doc.add_argument("--url", help="a running relay to check, e.g. http://127.0.0.1:8080")
    doc.add_argument("--public-url", help="the address senders use, e.g. https://relay.example.com")

    st = sub.add_parser("setup", help="read and write the config files (JSON answers; never switches to live)")
    st_sub = st.add_subparsers(dest="setup_action", required=True)
    st_sub.add_parser("show", help="the config on disk, with which variables are set (never their values)")
    st_sub.add_parser("catalog", help="the exchanges and what each can do")
    for action in ("write", "remove"):
        a = st_sub.add_parser(action)
        a.add_argument("kind", choices=["deployment", "source", "sink"] if action == "write" else ["source", "sink"])
        a.add_argument("name")
        if action == "write":
            a.add_argument("--file", required=True, help="YAML or JSON settings; - reads standard input")

    sub.add_parser("secret", help="set secrets at the terminal, never shown to anyone else (relay secret -h)")

    sub.add_parser("version", help="print the version")

    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["secret"]:                     # its own parser (relay/envfile.py, standard library only)
        from relay.envfile import main as secret_main
        return secret_main(argv[1:])
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "serve":
        import uvicorn

        from relay.api.app import create_app
        from relay.config import settings
        from relay.logging_setup import route_stdlib_logging

        if args.host not in ("127.0.0.1", "localhost", "::1") and not settings.api_key:
            # Development mode switches authentication off; on a reachable address that would
            # expose order placement and the emergency stop to the network.
            print(f"refusing to serve on {args.host} without RELAY_API_KEY: every endpoint, including "
                  f"order placement, would be open to the network", file=sys.stderr)
            return 2
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            short = [n for n in ("api_key", "api_readonly_key")
                     if 0 < len(getattr(settings, n)) < MIN_KEY_LENGTH]
            if short:
                # A short key can be guessed from the network despite the lockout (many addresses).
                print(f"refusing to serve on {args.host}: RELAY_{', RELAY_'.join(n.upper() for n in short)} "
                      f"must be at least {MIN_KEY_LENGTH} characters (e.g. the output of: "
                      f"openssl rand -hex 32)", file=sys.stderr)
                return 2
        route_stdlib_logging()
        # proxy_headers=False: the client address is the real peer. Which forwarded header to trust,
        # and from which proxies, is the relay's setting (RELAY_TRUSTED_PROXIES) for the API and each
        # source's own (trusted_proxies, client_ip_header) for signals.
        # limit_concurrency: past that many connections at once, new ones get 503 instead of
        # slowing everything down.
        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False,
                    limit_concurrency=settings.max_connections or None)
        return 0
    if args.command == "db":
        from relay.config import settings
        if args.action == "backup":
            return _backup_db(settings.database_url)
        from relay.runtime import upgrade_schema

        upgrade_schema(settings.database_url)
        print("database is at the latest schema")
        return 0
    if args.command == "check-config":
        return _check_config()
    if args.command == "test-signal":
        return _test_signal(args)
    if args.command == "verify-venue":
        return _verify_venue(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "setup":
        return _setup(args)
    if args.command == "version":
        print(f"quanti-relay {__version__}")
    return 0



def _doctor(args) -> int:
    import asyncio
    import json

    from relay import doctor
    from relay.config import settings

    checks, dep = doctor.offline_checks(settings)

    async def online():
        extra = []
        if args.online:
            extra += await doctor.key_checks(dep, settings, args.server_ip or settings.public_ip or None)
        if args.url:
            extra.append(await doctor.http_check("relay_running", args.url))
        if args.public_url:
            if not args.public_url.startswith("https://"):
                extra.append({"check": "public_address", "status": "fail", "detail": "the public address must be "
                              "https:// (TradingView sends to port 443 only)", "fix": "R7.3: the TLS proxy"})
            else:
                extra.append(await doctor.http_check("public_address", args.public_url))
        return extra

    checks += asyncio.run(online())
    ok = not any(c["status"] == "fail" for c in checks)
    if args.json:
        print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        print(doctor.render(checks))
    return 0 if ok else 1


def _setup(args) -> int:
    """`relay setup ...`: the console's setup, from the command line. Answers are JSON."""
    import json

    from relay import setup
    from relay.config import settings
    from relay.deployment_config import read_yaml

    def answer(obj, code=0):
        print(json.dumps(obj, indent=2, default=str))
        return code

    config_dir = Path(settings.config_dir)
    if args.setup_action == "catalog":
        return answer(setup.catalog())
    if args.setup_action == "show":
        disk = setup.on_disk(config_dir)
        dep = disk["deployment"]
        return answer({
            "config_dir": str(config_dir), "writable": setup.writable(config_dir),
            "deployment": None if dep is None else dep.model_dump(mode="json"),
            "deployment_problem": disk["deployment_problem"],
            "paper": bool(settings.dry_run) or (dep is not None and dep.dry_run),     # Runtime.is_dry_run
            "keys": setup.key_status(dep), "missing_limits": setup.missing_limits(dep),
            "sources": disk["sources"], "sinks": disk["sinks"]})
    try:
        if args.setup_action == "remove":
            setup.remove_config(config_dir, args.kind, args.name)
            return answer({"ok": True, "removed": f"{args.kind} {args.name}", "restart_needed": True})
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        data = read_yaml(text)
        before = _file_text(config_dir, args.kind, args.name)
        path = setup.write_config(config_dir, args.kind, args.name, data)
        changed = path.read_text() != before
        out = {"ok": True, "path": str(path), "changed": changed, "restart_needed": changed}
        if args.kind == "deployment":
            out["dry_run"] = read_yaml(path.read_text()).get("dry_run")
        return answer(out)
    except (setup.SetupError, OSError, ValueError) as e:
        return answer({"ok": False, "error": str(e)}, 1)


def _file_text(config_dir, kind: str, name: str) -> str | None:
    from relay import setup
    folder = config_dir / setup.KINDS[kind]
    paths = (sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]) if kind == "deployment" and folder.is_dir()
             else [folder / f"{name}.yaml"])
    path = next((p for p in paths if p.is_file()), None)
    return None if path is None else path.read_text()


def _backup_db(url: str) -> int:
    from relay.db.backup import backup, sqlite_path

    path = sqlite_path(url)
    if path is None:
        print("the relay copies SQLite databases only: back Postgres up with pg_dump", file=sys.stderr)
        return 2
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    print(f"copied to {backup(path)}")
    return 0


def _verify_venue(args) -> int:
    import asyncio

    from relay.config import settings
    from relay.deployment_config import DeploymentConfigError, load_deployment
    from relay.venue.factory import VenueConfigError, build_ccxt_venue, close_client
    from relay.venue.verify import ladder_offered, passed, render, verify_venue

    try:
        dep = load_deployment(settings.config_dir)
    except DeploymentConfigError as e:
        print(f"deployment: PROBLEM - {e}")
        return 1
    if dep is None or dep.venue.native:
        print("verify-venue checks a CCXT exchange: set venue.exchange (and venue.environment) in the deployment "
              "file. Bybit's native connector is checked by the manual round.")
        return 1
    v = dep.venue
    if v.environment == "live":
        print(f"verify-venue never runs on a live account: set venue.environment to a test environment of "
              f"{v.exchange} (docs/venues.md). An exchange without one stays a community connector.")
        return 1
    if args.notional <= 0 or args.stop_pct <= 0:
        print("--notional and --stop-pct must be above 0")
        return 1
    where = f"{v.exchange}{' spot' if v.market == 'spot' else ''} ({v.environment})"
    if not args.confirm:
        print(f"would trade on {where}: buy about {args.notional} USDT of {args.symbol}, stop {args.stop_pct}% "
              f"below, move it, then close and clean up. Nothing was sent; add --confirm to run it.")
        return 0

    def build():
        return build_ccxt_venue(v.exchange, v.environment, paper=False, market=v.market)

    async def run():
        venue = build()
        try:
            for note in await venue.prepare():
                print(f"note: {note}")

            async def rebuild():
                return build()
            return await verify_venue(venue, args.symbol.upper(), args.notional, args.stop_pct, rebuild,
                                      can_ladder=ladder_offered(venue))
        finally:
            await close_client(venue)

    try:
        steps = asyncio.run(run())
    except VenueConfigError as e:
        print(f"venue: PROBLEM - {e}")
        return 1
    print(f"verify-venue on {where}, {args.symbol.upper()}:")
    print(render(steps))
    ok = passed(steps)
    print("RESULT: all steps passed - record this run in docs/venues.md and mark the profile verified" if ok
          else "RESULT: FAILED - the connector is not ready for this exchange")
    return 0 if ok else 1


def _check_config() -> int:
    from relay.config import settings
    from relay.deployment_config import DeploymentConfigError, env_settings_ignored, load_deployment
    from relay.signals.sources import SourceConfigError, load_sources

    ok = True
    try:
        dep = load_deployment(settings.config_dir)
    except DeploymentConfigError as e:
        print(f"deployment: PROBLEM - {e}")
        ok, dep = False, None
    else:
        if dep is None:
            print(f"deployment: no file in {settings.config_dir}/deployments - the RELAY_* environment "
                  f"settings apply (deployment '{settings.deployment_name}')")
        else:
            print(f"deployment: {dep.name} - entries {'enabled' if dep.enabled else 'DISABLED'}")
            ignored = env_settings_ignored(settings)
            if ignored:
                print(f"  note: the file overrides RELAY_{', RELAY_'.join(k.upper() for k in ignored)}")
    if ok:
        # The same rule as the relay: paper if EITHER the environment or the file says dry-run.
        paper = settings.dry_run or (dep is not None and dep.dry_run)
        if dep is not None and not dep.venue.native:
            ok = _check_venue(dep, paper) and ok
        else:
            host = "demo" if settings.bybit_demo else "testnet" if settings.testnet else "MAINNET (real money)"
            print(f"mode: {'paper (dry-run): no order reaches the venue' if paper else 'LIVE'} - venue: bybit, "
                  f"host: {host}")
    try:
        sources = load_sources(settings.config_dir)
    except SourceConfigError as e:
        print(f"sources: PROBLEM - {e}")
        ok = False
    else:
        if not sources:
            print(f"sources: none in {settings.config_dir}/sources - POST /v1/signals refuses everything")
        name = dep.name if dep else settings.deployment_name
        for src in sources.values():
            reach = "" if src.may_reach(name) else f" (not bound to the running deployment {name})"
            print(f"source: {src.name} -> {', '.join(src.deployments)}{reach}"
                  f"{'' if src.enabled else ' - DISABLED'}")
    from relay.sinks import SinkConfigError, load_sinks
    try:
        sinks = load_sinks(settings.config_dir)
    except SinkConfigError as e:
        print(f"sinks: PROBLEM - {e}")
        ok = False
    else:
        if not sinks:
            print(f"sinks: none in {settings.config_dir}/sinks - events are kept (GET /v1/events), not sent")
        for snk in sinks:
            print(f"sink: {snk.name} ({snk.type}) - {snk.min_severity} and above"
                  + (f", only {', '.join(snk.kinds)}" if snk.kinds else ""))
    print("config OK" if ok else "config has problems: the relay will not start with it")
    return 0 if ok else 1


def _test_signal(args) -> int:
    """A signed signal, always dry_run: true (it can never trade live), sent to a running relay."""
    import json
    import uuid
    from datetime import UTC, datetime

    import httpx

    from relay.config import settings
    from relay.deployment_config import load_deployment
    from relay.signals.contract import parse_signal
    from relay.signals.sources import SIGNATURE_HEADER, SIGNATURE_WINDOW_S, load_sources

    try:
        source = load_sources(settings.config_dir).get(args.source)
        dep = load_deployment(settings.config_dir)
    except ValueError as e:                      # a source or deployment file the relay cannot use
        print(f"config problem: {e}", file=sys.stderr)
        return 2
    if source is None:
        print(f"no source named {args.source!r} in {settings.config_dir}/sources", file=sys.stderr)
        return 2
    body: dict = {"version": "1", "id": f"test-{uuid.uuid4().hex[:12]}", "source": source.name,
                  "deployment": args.deployment or (dep.name if dep else settings.deployment_name),
                  "sent_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "kind": "event",
                  "action": args.action, "dry_run": True}
    if args.weights:
        try:
            pairs = [(k.strip(), float(v)) for k, v in (pair.split("=", 1) for pair in args.weights.split(","))]
        except ValueError:
            print("--weights: write it as SYMBOL=WEIGHT,SYMBOL=WEIGHT, e.g. BTCUSDT=0.05,ETHUSDT=-0.03", file=sys.stderr)
            return 2
        weights = dict(pairs)
        if len(weights) < len(pairs):
            print("--weights: a symbol is listed twice", file=sys.stderr)
            return 2
        if args.action != "open":
            print("--weights sends a target book: --action does not belong to it", file=sys.stderr)
            return 2
        body.update(kind="target", weights=weights)
        del body["action"]
        if args.stop_pct is not None:
            body["stop_pct"] = args.stop_pct
        if args.gross_leverage is not None:
            body["gross_leverage"] = args.gross_leverage
        if any(v is not None for v in (args.symbol, args.side, args.notional, args.close_pct, args.stop_price)):
            print("--weights sends a target book: --symbol/--side/--notional/--close-pct/--stop-price do not "
                  "belong to it", file=sys.stderr)
            return 2
    if not args.weights:                         # an event (a target book was built above)
        if args.symbol:
            body["symbol"] = args.symbol
        if args.side:
            body["side"] = args.side
        if args.notional is not None:
            body["size"] = {"notional_usd": args.notional}
        if args.close_pct is not None:
            body["size"] = {"close_pct": args.close_pct}
        if args.stop_price is not None:
            body["stop"] = {"price": args.stop_price}
        elif args.stop_pct is not None:
            body["stop"] = {"pct": args.stop_pct}
    try:
        parse_signal(body)
    except ValueError as e:
        print(f"not a valid signal: {e}", file=sys.stderr)
        return 2
    raw = json.dumps(body).encode()
    header = source.sign(raw)
    target = httpx.URL(args.url)
    if target.scheme != "https" and target.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: sending a signed request to {args.url} unencrypted - anyone on the way can read "
              f"and replay it for {SIGNATURE_WINDOW_S} s (use https)", file=sys.stderr)
    if args.print_only:
        print(f"POST {args.url}/v1/signals")
        print(f"{SIGNATURE_HEADER}: {header}")
        print(raw.decode())
        return 0
    try:
        r = httpx.post(f"{args.url}/v1/signals", content=raw, timeout=10,
                       headers={"Content-Type": "application/json", SIGNATURE_HEADER: header})
    except httpx.HTTPError as e:
        print(f"could not reach the relay at {args.url}: {e}", file=sys.stderr)
        return 1
    print(f"{r.status_code} {r.text}")
    return 0 if r.status_code == 202 and r.json().get("status") == "accepted" else 1


if __name__ == "__main__":
    sys.exit(main())
