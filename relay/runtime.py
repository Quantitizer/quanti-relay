"""Process runtime: database, exchange client, the default deployment's engine and background jobs.

Startup order:
  1. database schema (Alembic upgrade to head)
  2. the built-in default account + deployment rows (deployment config files replace this in R2)
  3. the private WebSocket feed, when exchange keys are present (registered before the engine
     starts, so no venue event is missed)
  4. the execution engine (reconcile + import, then the position-sync and stop-audit loops), and
     the signal sources (config/sources/*.yaml; a bad source file stops the start)
  5. the PnL reconciliation loop (writes venue-verified PnL; report-only if `pnl_reconcile_writes`
     is switched off)

Nothing here can switch a deployment to live trading: the dry-run flag is read from settings on
every order, and the default is dry-run on testnet.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from relay.config import Settings
from relay.config import settings as default_settings
from relay.db import Database
from relay.db.models import Account, Deployment, Position
from relay.deployment_config import (
    DeploymentConfigError,
    DeploymentFile,
    env_settings_ignored,
    load_deployment,
)
from relay.events import EventLog
from relay.execution.engine import ExecutionEngine
from relay.execution.pnl_reconciler import reconcile_closed_pnl, reconcile_partial_pnl
from relay.signals.executor import EventExecutor
from relay.signals.log import SignalLog
from relay.signals.sources import Source, load_sources
from relay.signals.target_executor import TargetExecutor
from relay.sinks import Notifier, Sink, load_sinks
from relay.venue.factory import VenueConfigError, build_ccxt_venue, client_problems

# On shutdown, signals still being carried out get this long to finish (they may be placing orders:
# cut off half-way, a position could be left without its stop).
SIGNAL_DRAIN_S = 30
NOTIFY_FLUSH_S = 10            # how long shutdown waits for the last notifications


def upgrade_schema(url: str) -> None:
    """Alembic upgrade to head. Synchronous (Alembic's env runs its own event loop) — call it from a
    worker thread when a loop is already running."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "relay" / "db" / "migrations"))
    cfg.attributes["url"] = url
    _backup_before_upgrade(url, ScriptDirectory.from_config(cfg).get_current_head())
    command.upgrade(cfg, "head")


def _backup_before_upgrade(url: str, head: str | None) -> None:
    """A copy of the database before its schema changes, so a failed or unwanted upgrade can be undone."""
    from relay.db.backup import backup, current_revision, sqlite_path

    path = sqlite_path(url)
    if path is None:
        if not url.startswith("sqlite"):
            logger.warning("[Runtime] Postgres: back the database up with pg_dump before an upgrade; "
                           "the relay copies SQLite databases only")
        return
    revision = current_revision(path)
    if revision is None or revision == head:
        return                                        # new database, or nothing to upgrade
    copy = backup(path, revision)
    logger.info(f"[Runtime] schema upgrade from {revision}: the database was copied to {copy}")


def _has_keys(client) -> bool:
    """Whether the venue client was given keys (the same reading as the engine's account_readable)."""
    has = getattr(client, "has_keys", None)
    if has is None:
        has = getattr(client, "api_key", True)
    return bool(has)


class Runtime:
    def __init__(self, cfg: Settings | None = None, db: Database | None = None, client: Any = None,
                 private_ws_factory=None):
        """`db` and `client` may be injected (tests); otherwise they are built from settings."""
        self.cfg = cfg or default_settings
        self._own_db = db is None
        self._own_client = client is None
        self.db = db
        self.client = client
        self._private_ws_factory = private_ws_factory
        self.engine: ExecutionEngine | None = None
        self.signal_log: SignalLog | None = None
        self.private_ws = None
        self.deployment_id: int | None = None
        self.sources: dict[str, Source] = {}
        self.deployment: DeploymentFile | None = None   # config/deployments/<name>.yaml, if any
        self.event_executor: EventExecutor | None = None
        self.target_executor: TargetExecutor | None = None
        self.events: EventLog | None = None
        self.sinks: list[Sink] = []
        self.notifier: Notifier | None = None
        self._notifying: asyncio.Future | None = None
        self._tasks: list[asyncio.Task] = []
        self._signal_tasks: set[asyncio.Task] = set()
        self._stuck_reported: list[str] = []

    @property
    def venue(self) -> tuple[str, str]:
        """(venue id, environment): bybit's host from the environment settings, any other
        venue's from the deployment file's venue section (okxspot for OKX's spot market)."""
        if self.deployment is not None and not self.deployment.venue.native:
            return self.deployment.venue.venue_id, self.deployment.venue.environment
        return "bybit", "demo" if self.cfg.bybit_demo else "testnet" if self.cfg.testnet else "live"

    @property
    def deployment_name(self) -> str:
        return self.deployment.name if self.deployment is not None else self.cfg.deployment_name

    def execution_settings(self) -> dict:
        c = self.cfg
        loops = {
            "position_sync_interval_s": c.position_sync_interval_s,
            "stop_audit_interval_s": c.stop_audit_interval_s,
            "stop_rules_interval_s": c.stop_rules_interval_s,
        }
        if self.deployment is not None:
            return {**self.deployment.execution_settings(), **loops}
        return {
            "enabled": True,
            "leverage": c.default_leverage,
            "isolated_margin": c.isolated_margin,
            "require_stop": c.require_stop,
            "max_positions": c.max_positions,
            "max_exposure_pct": c.max_exposure_pct,
            "daily_loss_limit_pct": c.daily_loss_limit_pct,
            "min_balance": c.min_balance,
            "cooldown_hours": c.cooldown_hours,
            "min_stop_distance_pct": c.min_stop_distance_pct,
            "max_stop_distance_pct": c.max_stop_distance_pct,
            "take_profit_pct": c.take_profit_pct,
            "take_profit_ladder": c.take_profit_ladder,
            "trailing_distance_pct": c.trailing_distance_pct,
            "trailing_activate_pct": c.trailing_activate_pct,
            "break_even_trigger_pct": c.break_even_trigger_pct,
            "break_even_trigger_r": c.break_even_trigger_r,
            "break_even_buffer_pct": c.break_even_buffer_pct,
            "break_even_trigger_tp": c.break_even_trigger_tp,
            "max_size_shortfall_pct": c.max_size_shortfall_pct,
            "max_hold_hours": c.max_hold_hours,
            **loops,
        }

    def is_dry_run(self) -> bool:
        # Paper when EITHER the environment or the deployment file says so: going live takes both.
        return bool(self.cfg.dry_run) or (self.deployment is not None and self.deployment.dry_run)

    async def _ensure_default_deployment(self) -> int:
        async with self.db.session() as s:
            # One deployment runs. Open positions under another name would be invisible to it -
            # to its emergency stop and close-all included - so the start is refused instead.
            others = (await s.execute(
                select(Deployment.name, func.count(Position.id))
                .join(Position, Position.deployment_id == Deployment.id)
                .where(Deployment.name != self.deployment_name, Position.status.in_(("OPEN", "CLOSING")))
                .group_by(Deployment.name))).all()
            if others:
                listed = ", ".join(f"{n} ({c} open)" for n, c in others)
                raise DeploymentConfigError(
                    f"open positions belong to deployment {listed}, but the relay would run "
                    f"'{self.deployment_name}': it could not see, protect or close them. Name the "
                    f"deployment the same (name: in the deployment file, or RELAY_DEPLOYMENT_NAME), or "
                    f"close those positions first")
            acc = (await s.execute(select(Account).where(Account.name == "default"))).scalar_one_or_none()
            if acc is None:
                acc = Account(name="default", venue="bybit")
                s.add(acc)
                await s.flush()
            exchange, environment = self.venue
            before = (acc.venue or "bybit", "demo" if acc.demo else "testnet" if acc.testnet else "live")
            if before != (exchange, environment):
                # Positions opened on the old venue would be invisible on the new one: the sync
                # would record them as closed while they stay open there, unprotected.
                open_live = (await s.execute(
                    select(func.count(Position.id)).join(Deployment, Deployment.id == Position.deployment_id)
                    .where(Deployment.account_id == acc.id, Position.status.in_(("OPEN", "CLOSING")),
                           Position.dry_run.is_(False)))).scalar() or 0
                if open_live:
                    raise VenueConfigError(
                        f"{open_live} live position(s) are open on {before[0]} ({before[1]}), but the relay is now "
                        f"set to {exchange} ({environment}): it could not see, protect or close them. Switch "
                        f"back, or close them first")
            acc.venue = exchange
            acc.demo, acc.testnet = environment == "demo", environment == "testnet"
            if exchange == "bybit":
                acc.testnet, acc.demo = bool(self.cfg.testnet), bool(self.cfg.bybit_demo)
            dep = (await s.execute(select(Deployment).where(
                Deployment.name == self.deployment_name))).scalar_one_or_none()
            if dep is None:
                # A random id, not 1: it goes into every order's tag (rl-d<id>-...), and two relays
                # on one exchange account must not take each other's positions for their own.
                taken = set((await s.execute(select(Deployment.id))).scalars().all())
                new_id = next(i for i in iter(lambda: 1000 + secrets.randbelow(999_000), None) if i not in taken)
                dep = Deployment(id=new_id, name=self.deployment_name, account_id=acc.id)
                s.add(dep)
            dep.dry_run = self.is_dry_run()   # mirrors the setting, for display; orders read the setting
            await s.commit()
            return dep.id

    async def start(self) -> None:
        # First: a bad config file stops the start before anything else happens.
        self.deployment = load_deployment(self.cfg.config_dir)
        self.sources = load_sources(self.cfg.config_dir)
        self.sinks = load_sinks(self.cfg.config_dir)
        if self.deployment is not None:
            ignored = env_settings_ignored(self.cfg)
            if ignored:
                logger.warning(f"[Runtime] the deployment file {self.deployment.name} sets the trading settings: "
                               f"RELAY_{', RELAY_'.join(k.upper() for k in ignored)} in the environment "
                               f"are ignored")
        if self.db is None:
            await asyncio.to_thread(upgrade_schema, self.cfg.database_url)
            self.db = Database(self.cfg.database_url)
        else:
            await self.db.create_all()
        self.deployment_id = await self._ensure_default_deployment()

        exchange, environment = self.venue
        if self.client is None and exchange != "bybit":
            v = self.deployment.venue
            self.client = build_ccxt_venue(v.exchange, environment, paper=self.is_dry_run(), market=v.market)
        if exchange != "bybit" or getattr(self.client, "profile", None) is not None:
            refusals, warnings = client_problems(self.client, self.execution_settings())
            if refusals:
                raise VenueConfigError(f"{exchange}: " + "; ".join(refusals))
            warnings += await self.client.prepare()
            for w in warnings:
                logger.warning(f"[Venue] {w}")
        if self.client is None:
            from relay.exchange.bybit.client_factory import get_bybit_client
            # This runtime's own settings decide the venue host (not the process-wide defaults).
            self.client = get_bybit_client(demo=bool(self.cfg.bybit_demo), testnet=bool(self.cfg.testnet))
        if self.is_dry_run() and not _has_keys(self.client):
            # Paper without keys: no account to read, so the balance is the owner's paper_equity.
            from relay.venue.paper import PaperAccount
            self.client = PaperAccount(self.client, self.deployment.paper_equity if self.deployment else None)

        self.signal_log = SignalLog(self.db)
        await self.signal_log.fail_unfinished(self.deployment_name)
        self.engine = ExecutionEngine(
            self.db, self.client, self.execution_settings,
            dry_run=self.is_dry_run, deployment_id=self.deployment_id, signal_log=self.signal_log,
        )

        self.events = EventLog(self.db, self.deployment_id)
        self.engine.events = self.events
        self.notifier = Notifier(self.events, self.sinks)
        await self.notifier.start_cursors()          # before the first event, so it is delivered
        self.event_executor = EventExecutor(self.engine, self.signal_log, self.events)
        self.target_executor = TargetExecutor(self.engine, self.signal_log, self.events)

        if self.cfg.private_ws_enabled and exchange == "bybit":     # Bybit's own live feed
            self._start_private_ws()

        await self.engine.start()
        self._tasks.append(asyncio.create_task(self._pnl_audit_loop()))
        self._tasks.append(asyncio.create_task(self._notify_loop()))
        if self.cfg.heartbeat_url:
            self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        host = f"{exchange} {environment}" if exchange != "bybit" else (
            "demo" if self.cfg.bybit_demo else "testnet" if self.cfg.testnet else "mainnet")
        await self.events.record("relay.started", "info",
                                 f"started: deployment {self.deployment_name}, "
                                 f"{'paper (dry-run)' if self.is_dry_run() else 'LIVE'}, venue host {host}",
                                 {"deployment": self.deployment_name, "paper": self.is_dry_run(), "host": host})
        logger.info(
            f"[Runtime] started: deployment={self.deployment_name} "
            f"dry_run={self.is_dry_run()} testnet={self.cfg.testnet} demo={self.cfg.bybit_demo}"
        )

    def _start_private_ws(self) -> None:
        factory = self._private_ws_factory
        if factory is None:
            if not (os.environ.get("BYBIT_API_KEY") and os.environ.get("BYBIT_API_SECRET")):
                logger.warning("[Runtime] no exchange keys in the environment: private feed off")
                return
            from relay.exchange.bybit.private_ws import BybitPrivateWsClient

            def factory():
                return BybitPrivateWsClient(demo=bool(self.cfg.bybit_demo), testnet=bool(self.cfg.testnet))
        try:
            self.private_ws = factory()
            self.engine.set_private_ws(self.private_ws)
            self._tasks.append(asyncio.create_task(self.private_ws.start()))
        except Exception as e:
            logger.error(f"[Runtime] private feed failed to start (polling only): {e}")
            self.private_ws = None

    async def pnl_audit(self) -> dict:
        """One pass of the PnL reconciler (report-only unless pnl_reconcile_writes)."""
        writes = bool(self.cfg.pnl_reconcile_writes)
        closes = await reconcile_closed_pnl(self.db, self.client, dry_run=not writes)
        partials = await reconcile_partial_pnl(self.db, self.client, dry_run=not writes)
        return {**closes, "partials": partials}

    def health(self) -> dict:
        """What /health answers: ok, starting, halted (the emergency stop) or stuck (a background
        loop ended, hangs or fails every round: positions and stops are not being watched)."""
        if self.engine is None:
            return {"status": "starting"}
        if self.engine.halted_reason:
            return {"status": "halted", "reason": self.engine.halted_reason}
        stuck = self.engine.stuck_loops()
        if stuck:
            return {"status": "stuck", "loops": stuck}
        return {"status": "ok"}

    async def _report_stuck(self) -> None:
        """An event when a background loop gets stuck (once until it recovers), so the sinks say so."""
        stuck = [] if self.engine is None else self.engine.stuck_loops()
        if stuck and stuck != self._stuck_reported:
            await self.events.record("relay.stuck", "critical",
                                     f"background work stopped: {', '.join(stuck)}; positions and stops "
                                     f"may not be watched. Restart the relay.", {"loops": stuck})
        self._stuck_reported = stuck

    async def _heartbeat_loop(self) -> None:
        """The dead-man switch: call heartbeat_url while healthy. The service behind it alerts the
        owner when the calls stop, which covers what the relay cannot report itself: it is down,
        its server is gone, or it is stuck."""
        import httpx

        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
            while True:
                if self.health()["status"] == "ok":
                    try:
                        r = await http.get(self.cfg.heartbeat_url)
                        if r.status_code >= 300:
                            logger.warning(f"[Runtime] heartbeat answered {r.status_code}")
                    except httpx.HTTPError as e:
                        logger.warning(f"[Runtime] heartbeat failed: {type(e).__name__}")
                await asyncio.sleep(max(self.cfg.heartbeat_interval_s, 1))

    async def _pnl_audit_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.pnl_reconcile_interval_s)
            if self.engine is not None and not self.engine.account_readable():
                continue                                  # no keys: no venue records to read
            try:
                await self.pnl_audit()
            except Exception as e:
                logger.error(f"[Runtime] PnL audit failed: {e}")

    def run_signal(self, coro) -> asyncio.Task:
        """Carry out an accepted signal in the background (its sender has its answer already)."""
        task = asyncio.create_task(coro)
        self._signal_tasks.add(task)
        task.add_done_callback(self._signal_tasks.discard)
        return task

    async def _notify_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.notify_interval_s)
            await self._report_stuck()
            try:
                # Shielded: a shutdown never cuts a send in half (it would be sent again after
                # the restart); stop() waits for the round instead.
                await asyncio.shield(self._notify_round())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Runtime] notification round failed: {e}")

    async def _notify_round(self) -> None:
        self._notifying = asyncio.ensure_future(self.notifier.deliver_once())
        try:
            await self._notifying
        finally:
            self._notifying = None

    async def _flush_notifications(self) -> None:
        """At shutdown: finish the round under way, then one more for what the shutdown reported."""
        if self.notifier is None:
            return
        if self._notifying is not None:
            await asyncio.wait({self._notifying}, timeout=NOTIFY_FLUSH_S)
        try:
            await asyncio.wait_for(self.notifier.deliver_once(), timeout=NOTIFY_FLUSH_S)
        except TimeoutError:
            logger.warning("[Runtime] notifications not all sent at shutdown - the rest go after the restart")

    async def stop(self) -> None:
        if self._signal_tasks:
            pending = list(self._signal_tasks)
            logger.info(f"[Runtime] waiting for {len(pending)} signal(s) still being carried out")
            _, late = await asyncio.wait(pending, timeout=SIGNAL_DRAIN_S)
            for t in late:
                logger.error("[Runtime] a signal was still being carried out at shutdown - cancelled; "
                             "check its position")
                t.cancel()
            await asyncio.gather(*late, return_exceptions=True)
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Each step runs even if an earlier one fails: nothing is left open behind a shutdown error.
        steps = [
            ("engine", self.engine.stop if self.engine is not None else None),
            ("private feed", self.private_ws.stop if self.private_ws is not None else None),
            ("venue client", self.client.close if self._own_client and hasattr(self.client, "close") else None),
            ("notifications flush", self._flush_notifications),
            ("notifications", self.notifier.close if self.notifier is not None else None),
            ("database", self.db.close if self._own_db and self.db is not None else None),
        ]
        for name, step in steps:
            if step is None:
                continue
            try:
                await step()
            except Exception as e:
                logger.warning(f"[Runtime] stopping the {name} failed: {e}")
