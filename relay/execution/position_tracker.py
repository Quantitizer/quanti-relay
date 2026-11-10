"""
Position tracker - the relay's book of positions, kept honest against the exchange.

- `get_all_positions`: open positions with their latest stop info.
- `sync_all_positions`: reconcile open rows with the exchange (close what is gone, refresh PnL).
- `import_exchange_positions`: adopt exchange positions that have no row yet.

The exchange is the authority on what is held. A failed or partial exchange read never closes a
row: only a successful read that no longer shows the position does.
"""
import json
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from relay.exchange.bybit.rest_client import BybitRestClient
from relay.execution.close_record import price_close, write_close
from relay.execution.order_link import parse_order_link
from relay.venue import venue_of


def _default_account_deployments(venue: str) -> str:
    """Deployments on the process-wide (default) account of this venue: no per-account keys."""
    return ("SELECT d.id FROM deployments d JOIN accounts a ON a.id = d.account_id "
            f"WHERE a.venue = '{venue}' AND a.credentials_ref IS NULL")


# The reason on the stub trade an import writes; the engine removes such a stub when its own
# entry record wins a race with the import.
IMPORT_TRADE_REASON = "imported (reconciled)"

class PositionTracker:
    """
    Tracks open positions and reconciles them with the exchange.
    """
    
    def __init__(
        self,
        db_pool,
        bybit_client: BybitRestClient,
        default_leverage: float | None = None,
        busy=None,
    ):
        """
        Initialize position tracker.
        
        Args:
            db_pool: Database (relay.db.Database)
            bybit_client: Bybit REST client of the default account
            default_leverage: the user's configured leverage, if any. Never invented: when neither the
                position, the venue nor the user states one, leverage stays unknown (None) and PnL% is
                the % of notional.
        """
        self.db = db_pool
        self.bybit = bybit_client
        self.default_leverage = default_leverage
        # busy(position_id) -> True while the engine is closing that position itself: the sync
        # leaves it to the engine (which records the real fill).
        self.busy = busy or (lambda _pid: False)
    
    async def _drop_vanished_trailing(self, db_pos: dict[str, Any], venue_has_trailing: bool | None,
                                      fetched_at: datetime) -> None:
        """A trailing stop the relay recorded but the venue no longer shows (removed on the venue)
        is dropped from the record. Only one recorded before the venue was read: one set while the
        sync ran is not in that reading yet."""
        if venue_has_trailing is not False or not db_pos.get('management'):
            return
        async with self.db.acquire() as conn, conn.transaction():
            # Re-read: the record may have changed since the sync started.
            management = await conn.fetchval("SELECT management FROM positions WHERE id = $1", db_pos['id'])
            if isinstance(management, str):
                management = json.loads(management) if management else None
            trailing = (management or {}).get('trailing')
            if not trailing:
                return
            try:
                set_at = datetime.fromisoformat(trailing.get('set_at'))
            except (TypeError, ValueError):
                set_at = None
            if set_at is None or set_at >= fetched_at:
                return
            rest = {k: v for k, v in management.items() if k != 'trailing'}
            await conn.execute(
                "UPDATE positions SET management = $1 WHERE id = $2 AND status IN ('OPEN', 'CLOSING')",
                json.dumps(rest) if rest else None, db_pos['id'])
        logger.warning(f"[PositionTracker] {db_pos['symbol']} #{db_pos['id']}: the trailing stop is no longer "
                       f"on the venue (removed there) - record updated")

    async def get_all_positions(self) -> list[dict[str, Any]]:
        """
        Get all open positions from database with latest stop-loss info.
        
        JOINs with trades to get stop_loss_price, stop_loss_order_id, and trade_type
        from the most recent trade for each position.
        
        Returns:
            List of position dicts including stop_loss_price from trades
        """
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 
                    p.*,
                    t.stop_loss_price,
                    t.stop_loss_order_id,
                    t.trade_type
                FROM positions p
                LEFT JOIN trades t ON t.id = (
                    SELECT id FROM trades 
                    WHERE position_id = p.id 
                    ORDER BY time DESC 
                    LIMIT 1
                )
                WHERE p.status IN ('OPEN', 'CLOSING')
                ORDER BY p.opened_at DESC
                """
            )
            return [dict(row) for row in rows]

    async def restore_positions(self) -> None:
        """
        Restore open positions from database on startup.
        """
        positions = await self.get_all_positions()
        logger.info(f"[PositionTracker] Restored {len(positions)} open positions from database")

    async def sync_all_positions(self, client=None, scope_deployment_ids=None) -> dict[str, Any]:
        """
        Reconcile positions with exchange state.

        Multi-account: `client` is the Bybit client/adapter to reconcile against (default = self.bybit,
        the DEFAULT account). `scope_deployment_ids`:
          - None -> DEFAULT sync: reconcile ONLY default-account positions, and NEVER a keyed
            account's legs (they aren't in the default snapshot, so "not on exchange -> CLOSED"
            would FALSELY close them, and the strategy would re-open them on the next signal).
          - a set -> PER-ACCOUNT sync: reconcile ONLY positions of those deployments, against `client`
            (that account's adapter). This is how a non-default account's real closes get detected.

        Matches on (symbol, side) not just symbol for safety.
        Marks closed positions as CLOSED and updates PnL.
        
        IMPORTANT: Match on (symbol, side) instead of just symbol.
        Even in one-way mode, this is safer for future hedged mode support
        and handles edge cases where side might differ.
        
        Returns:
            Dict with sync results: {
                'synced': int,  # Total positions checked
                'closed': int,  # Positions marked as closed
                'updated': int, # Positions with PnL updates
                'errors': int,  # Number of errors
            }
        """
        try:
            client = client or self.bybit
            # Get all open positions from database
            db_positions = await self.get_all_positions()

            if not db_positions:
                return {'synced': 0, 'closed': 0, 'updated': 0, 'errors': 0}

            # ACCOUNT SCOPE (multi-account) — see the docstring. Default sync excludes keyed accounts;
            # a per-account sync includes ONLY its own deployments. `_in_scope(deployment_id)` gates every position.
            if scope_deployment_ids is None:
                # DEFAULT sync: include ONLY the default Bybit account (venue='bybit', no credentials_ref)
                # plus rows with no deployment. Everything else — a keyed Bybit account OR a different
                # venue (Hyperliquid) — is NOT on self.bybit's account and must be excluded, else its legs
                # get falsely CLOSED. Those are reconciled by their own per-account sync.
                async with self.db.acquire() as conn:
                    _dp = await conn.fetch(_default_account_deployments(venue_of(self.bybit)))
                _default_deployments = {r['id'] for r in _dp}
                def _in_scope(did):
                    return did is None or did in _default_deployments
            else:
                _scope = set(scope_deployment_ids)
                def _in_scope(did):
                    return did in _scope                # per-account: ONLY this account's deployments

            # Fetch ALL USDT perpetual positions from Bybit (of the account `client` points at)
            # SAFETY: If this call fails, we must NOT proceed to mark positions CLOSED.
            # Let the exception propagate so no positions are affected.
            fetched_at = datetime.now(UTC)
            try:
                bybit_positions = await client.get_positions(
                    category='linear',
                    settle_coin='USDT'
                )
            except Exception as e:
                logger.error(
                    f"[PositionTracker] sync_all_positions: cannot reach the venue ({e}). "
                    "Aborting sync — no positions will be marked CLOSED."
                )
                return {'synced': 0, 'closed': 0, 'updated': 0, 'errors': 1}
            
            # Build set of (symbol, side) tuples for open exchange positions
            exchange_open = set()
            # ORPHAN-FIX: symbol-level index. A DB row whose SIDE disagrees with the exchange (what a
            # flip leaves behind when the row for the new side was never written) misses the
            # (symbol, side) key and would be marked CLOSED while the position is still LIVE on the
            # other side — an orphan nothing would ever manage. Its fingerprint is
            # pnl_source='venue_unmatched': closed with no matching exchange close record.
            exchange_open_symbols = set()
            exchange_pnl_map = {}  # (symbol, side) -> unrealized_pnl
            exchange_stop_map = {}  # (symbol, side) -> the fixed stop set on the venue, None if none
            exchange_trailing_map = {}  # (symbol, side) -> True if the venue shows a trailing stop
            
            for p in bybit_positions:
                size = float(p.get('size', 0) or 0)
                # Size is absolute; the direction is `side` ('Buy' = long).
                if size != 0:
                    symbol = p['symbol']
                    # Map Bybit side to our side convention
                    bybit_side = p.get('side', '')
                    side = 'LONG' if bybit_side == 'Buy' else 'SHORT'
                    key = (symbol, side)
                    exchange_open.add(key)
                    exchange_open_symbols.add(symbol)
                    upnl = p.get('unrealisedPnl')
                    exchange_pnl_map[key] = float(upnl) if upnl not in (None, '') else None
                    try:
                        exchange_stop_map[key] = float(p.get('stopLoss') or 0) or None
                    except (TypeError, ValueError):
                        exchange_stop_map[key] = None
                    # None (field missing or unreadable) = unknown: the record is left alone.
                    try:
                        raw_trailing = p.get('trailingStop')
                        exchange_trailing_map[key] = (None if raw_trailing in (None, '')
                                                      else float(raw_trailing) > 0)
                    except (TypeError, ValueError):
                        exchange_trailing_map[key] = None
            
            closed_count = 0
            updated_count = 0
            error_count = 0
            side_mismatch_count = 0
            now = datetime.now(UTC)
            
            for db_pos in db_positions:
                try:
                    symbol = db_pos['symbol']
                    side = db_pos['side'].upper()
                    position_id = db_pos['id']
                    # Only reconcile positions on the account this `client` points at.
                    if not _in_scope(db_pos.get('deployment_id')):
                        continue
                    # Paper positions have nothing on the exchange to reconcile against.
                    if db_pos.get('dry_run'):
                        continue
                    # The engine is closing it itself and records the real fill: leave it.
                    if self.busy(position_id):
                        continue
                    db_key = (symbol, side)

                    if db_key not in exchange_open:
                        # Gone from the venue (closed there, or flipped to the other side: then
                        # THIS side was closed too, and the import adopts the new side). Recorded
                        # like every close (relay.execution.close_record): the venue's complete
                        # records if published, else 'pending' for the reconciler.
                        if symbol in exchange_open_symbols:
                            side_mismatch_count += 1
                            logger.warning(
                                f"[PositionTracker] {symbol}: DB {side} (id={position_id}) is gone but the "
                                f"venue holds the opposite side - closing this row; the import adopts the other"
                            )
                        closed_at = datetime.now(UTC)
                        priced = await price_close(self.db, client, db_pos, closed_at)
                        async with self.db.acquire() as conn:
                            wrote = await write_close(conn, db_pos, priced, closed_at, 'closed_on_venue')
                        if wrote:
                            closed_count += 1
                            logger.info(
                                f"[PositionTracker] Marked {symbol} {side} (id={position_id}) as CLOSED "
                                f"PnL {priced['pnl']} ({priced['pnl_source']})"
                            )
                    else:
                        # Position still open - update unrealized PnL. A CLOSING row that is
                        # still on the exchange means its close did not go through: back to OPEN,
                        # so it is protected, audited and closable again.
                        if db_pos.get('status') == 'CLOSING':
                            logger.warning(
                                f"[PositionTracker] {symbol} {side} (id={position_id}) was CLOSING but is "
                                f"still open on the exchange - the close did not happen; back to OPEN"
                            )
                        unrealized_pnl = exchange_pnl_map.get(db_key)
                        # The venue's fixed stop is the current one (a trailing stop never changes
                        # it). A venue with no stop leaves the recorded one, which the stop audit
                        # re-arms. A row written since the venue was read (e.g. a stop moved
                        # meanwhile) keeps its stop: this reading may predate the move.
                        async with self.db.acquire() as conn:
                            await conn.execute(
                                "UPDATE positions SET unrealized_pnl = $1, status = 'OPEN', updated_at = $2, "
                                "stop_price = CASE WHEN updated_at >= $5 THEN stop_price "
                                "ELSE coalesce($4, stop_price) END "
                                "WHERE id = $3 AND status IN ('OPEN', 'CLOSING')",
                                unrealized_pnl, now, position_id, exchange_stop_map.get(db_key), fetched_at,
                            )
                        await self._drop_vanished_trailing(db_pos, exchange_trailing_map.get(db_key), fetched_at)
                        updated_count += 1

                except Exception as e:
                    logger.error(f"[PositionTracker] Error syncing position {db_pos.get('id')}: {e}")
                    error_count += 1
            
            logger.info(
                f"[PositionTracker] Sync complete: "
                f"{len(db_positions)} checked, {closed_count} closed, "
                f"{updated_count} updated, {error_count} errors, "
                f"{side_mismatch_count} side flips"
            )

            return {
                'synced': len(db_positions),
                'closed': closed_count,
                'updated': updated_count,
                'errors': error_count,
                'side_mismatch': side_mismatch_count,
            }
            
        except Exception as e:
            logger.opt(exception=True).error(f"[PositionTracker] Error in sync_all_positions: {e!s}")
            return {'synced': 0, 'closed': 0, 'updated': 0, 'errors': 1}

    async def import_exchange_positions(self) -> dict[str, Any]:
        """
        Import existing positions from Bybit that are not in the database.
        
        This allows the platform to display and manage positions that were:
        - Opened manually on Bybit
        - Opened before the bot started
        - Opened by another system
        
        Returns:
            Dict with import results: {
                'imported_count': int,
                'total_exchange_positions': int,
                'already_tracked': int,
                'imported_symbols': List[str]
            }
        """
        try:
            # Fetch ALL USDT perpetual positions from Bybit
            bybit_positions = await self.bybit.get_positions(
                category='linear',
                settle_coin='USDT'
            )
            
            # Positions with a non-zero (absolute) size
            active_bybit_positions = [
                p for p in bybit_positions
                if abs(float(p.get('size', 0) or 0)) > 0
            ]
            
            if not active_bybit_positions:
                logger.info("[PositionTracker] No active positions on Bybit to import")
                return {
                    'imported_count': 0,
                    'total_exchange_positions': 0,
                    'already_tracked': 0,
                    'imported_symbols': []
                }

            # This importer reads the DEFAULT client only, so its dedup must ignore keyed
            # account rows (see the tracked_id query below). Resolved once per run.
            async with self.db.acquire() as _conn:
                _dp = await _conn.fetch(_default_account_deployments(venue_of(self.bybit)))
            _import_default_deployments = {r['id'] for r in _dp}
            
            logger.info(
                f"[PositionTracker] Bybit returned {len(bybit_positions)} total, "
                f"{len(active_bybit_positions)} with size>0"
            )

            # Get existing tracked symbols from database. ACCOUNT SCOPE: this import runs against the
            # DEFAULT account, so dedup ONLY against default-book rows — else a keyed account or another
            # venue holding the same symbol would wrongly block importing a real DEFAULT-account position.
            async with self.db.acquire() as conn:
                existing_rows = await conn.fetch(
                    f"""
                    SELECT symbol, side FROM positions
                    WHERE status IN ('OPEN', 'CLOSING') AND NOT dry_run
                      AND (deployment_id IS NULL OR deployment_id IN ({_default_account_deployments(venue_of(self.bybit))}))
                    """
                )
                existing = {(row['symbol'], row['side']) for row in existing_rows}

            # Positions to import: (symbol, side) not tracked yet. A venue position is one-way, so its
            # side is its direction ('Buy' = LONG).
            positions_to_import = [
                p for p in active_bybit_positions
                if (p.get('symbol'), 'LONG' if p.get('side') == 'Buy' else 'SHORT') not in existing
            ]
            already_tracked = len(active_bybit_positions) - len(positions_to_import)
            logger.info(
                f"[PositionTracker] tracked={len(existing)}, "
                f"to_import={len(positions_to_import)}, already_tracked={already_tracked}"
            )

            if not positions_to_import:
                logger.info(
                    f"[PositionTracker] All {len(active_bybit_positions)} Bybit positions "
                    "are already tracked in database"
                )
                return {
                    'imported_count': 0,
                    'total_exchange_positions': len(active_bybit_positions),
                    'already_tracked': already_tracked,
                    'imported_symbols': []
                }
            
            # Import new positions
            imported_symbols = []
            now = datetime.now(UTC)
            
            async with self.db.acquire() as conn:
                for bybit_pos in positions_to_import:
                    try:
                        symbol = bybit_pos.get('symbol')
                        size = float(bybit_pos.get('size', 0))
                        # Bybit REST uses avgPrice; some responses may use avgEntryPrice
                        avg_price = float(bybit_pos.get('avgPrice', 0) or bybit_pos.get('avgEntryPrice', 0))
                        bybit_side = bybit_pos.get('side', '')  # "Buy" or "Sell"
                        created_time = bybit_pos.get('createdTime')
                        upnl_raw = bybit_pos.get('unrealisedPnl')
                        unrealised_pnl = float(upnl_raw) if upnl_raw not in (None, '') else None
                        mark_price = float(bybit_pos.get('markPrice', 0) or 0)

                        # The entry is the venue's position average. A real position must be imported
                        # (so it is protected and audited) even if the venue omits that: the mark
                        # price is used then, and the row says so (entry_price_source).
                        entry_price_source = 'venue_position'
                        if avg_price <= 0:
                            if mark_price <= 0:
                                logger.error(f"[PositionTracker] {symbol}: cannot import - no entry or mark price")
                                continue
                            logger.warning(
                                f"[PositionTracker] {symbol}: venue reports no entry price - importing with "
                                f"the mark price {mark_price} (labelled mark_at_import)"
                            )
                            avg_price, entry_price_source = mark_price, 'mark_at_import'
                        
                        # Log the entry price we're using for debugging
                        logger.info(
                            f"[PositionTracker] {symbol}: Importing with entry_price=${avg_price:.4f} "
                            f"(markPrice=${mark_price:.4f}, unrealizedPnL={unrealised_pnl})"
                        )
                        
                        # Determine side: In one-way mode, side indicates position direction
                        # "Buy" = LONG, "Sell" = SHORT
                        if bybit_side == 'Buy':
                            side = 'LONG'
                        elif bybit_side == 'Sell':
                            side = 'SHORT'
                        else:
                            # Fallback: size > 0 typically means long in some contexts
                            side = 'LONG' if size > 0 else 'SHORT'
                        size = abs(size)
                        
                        # Use current time as opened_at since we're detecting this position now
                        # NOTE: Bybit's createdTime is unreliable - it represents when position
                        # tracking started, not when the actual trade was executed. For positions
                        # that have been averaged into, createdTime never updates.
                        opened_at = now
                        
                        # Log Bybit's createdTime for debugging purposes only
                        if created_time:
                            try:
                                bybit_created = datetime.fromtimestamp(
                                    int(created_time) / 1000, 
                                    tz=UTC
                                )
                                logger.debug(
                                    f"[PositionTracker] {symbol}: Bybit createdTime={bybit_created}, "
                                    f"using import time={opened_at} as opened_at"
                                )
                            except (ValueError, TypeError):
                                pass
                        
                        # Initial stop: the stop already set on the exchange, if any. A position
                        # without one is left for the stop audit, which applies the deployment's rule.
                        try:
                            initial_stop_price = float(bybit_pos.get('stopLoss') or 0) or None
                        except (TypeError, ValueError):
                            initial_stop_price = None
                        entry_meta = {
                            'source': 'import',
                            'imported_at': now.isoformat(),
                            'entry_price': avg_price,
                            'entry_price_source': entry_price_source,
                        }

                        # Dedup: the unique index is per deployment, so it does not enforce GLOBAL
                        # (symbol, side) uniqueness. A single exchange account nets ONE position per
                        # (symbol, side): skip the import if ANY OPEN default-account row already tracks
                        # this position (the executor wrote it, or a prior import did). Inserting a
                        # duplicate (deployment NULL) row next to a tracked row would later collide when
                        # the row is attributed. The ON CONFLICT below is only a same-key race backstop.
                        # ACCOUNT SCOPE: only the DEFAULT account's rows count. This importer only ever
                        # reads self.bybit (the default client), so a KEYED account's OPEN row for the
                        # same (symbol, side) is a DIFFERENT position on a DIFFERENT exchange account —
                        # letting it suppress this import would orphan a genuine default-account position.
                        _scope_ids = sorted(_import_default_deployments)
                        _in_list = ", ".join(f"${n}" for n in range(3, 3 + len(_scope_ids))) or "NULL"
                        # (named tracked_id: `already_tracked` is the per-run count returned below)
                        tracked_id = await conn.fetchval(
                            "SELECT id FROM positions WHERE symbol = $1 AND side = $2 "
                            "AND status = 'OPEN' AND NOT dry_run "
                            f"AND (deployment_id IS NULL OR deployment_id IN ({_in_list})) LIMIT 1",
                            symbol, side, *_scope_ids,
                        )
                        if tracked_id is not None:
                            logger.debug(
                                f"[PositionTracker] {symbol} {side}: skip import — OPEN position "
                                f"already tracked (id {tracked_id})"
                            )
                            continue

                        # DETERMINISTIC OWNERSHIP (no import race): the relay tags every position-OPENING
                        # order with an orderLinkId 'rl-d{deployment_id}-...'. Read it back off this
                        # symbol's recent order history so a position WE opened is attributed to its
                        # deployment at IMPORT time — even if the executor hasn't written the row yet.
                        # Falls back to no deployment only for a position opened OUTSIDE the relay.
                        imp_dep, imp_link = None, None
                        try:
                            # Newest first: the most recent relay-tagged OPENING order on this
                            # position's side (not a close, not the other direction).
                            opening_side = 'Buy' if side == 'LONG' else 'Sell'
                            hist = await self.bybit.get_order_history(symbol=symbol, limit=20) or []
                            for o in hist:
                                if o.get('side') != opening_side or o.get('reduceOnly') in (True, 'true'):
                                    continue
                                did = parse_order_link(o.get('orderLinkId'))
                                if did is not None:
                                    imp_dep, imp_link = did, str(o.get('orderLinkId'))
                                    break
                            if imp_dep is not None:
                                tagged = imp_dep
                                imp_dep = await conn.fetchval(
                                    "SELECT id FROM deployments WHERE id = $1", imp_dep)
                                if imp_dep is None:
                                    logger.warning(
                                        f"[PositionTracker] {symbol} {side}: opened by a relay deployment ({tagged}) "
                                        f"this database does not know - another relay on this exchange account, or "
                                        f"an older database. Give each relay its own account or subaccount")
                        except Exception as _e:
                            logger.warning(f"[PositionTracker] {symbol}: order-link attribution failed (-> none): {_e}")

                        # Our entry whose fill was never recorded (the process died in between): it
                        # was sent with a stop, written down before it went out. Some venues shorten
                        # the tag (relay.venue.ccxt_venue.encode_client_id), so it is matched by prefix.
                        if initial_stop_price is None and imp_dep is not None and imp_link:
                            try:
                                intended = await conn.fetchval(
                                    "SELECT stop_price FROM order_intents WHERE order_link LIKE $1 AND symbol = $2 "
                                    "AND side = $3 AND deployment_id = $4 ORDER BY created_at DESC LIMIT 1",
                                    imp_link + '%', symbol, side, imp_dep)   # tags hold no % or _
                                if intended:
                                    initial_stop_price = float(intended)
                                    entry_meta['stop_from'] = 'order_intent'
                            except Exception as _e:
                                logger.warning(f"[PositionTracker] {symbol}: the entry's intended stop could not be read: {_e}")

                        # Insert into positions and get the position ID
                        position_row = await conn.fetchrow(
                            """
                            INSERT INTO positions (
                                symbol, side, size, entry_price, status, opened_at,
                                unrealized_pnl, entry_meta, initial_stop_price, stop_price,
                                created_at, updated_at, deployment_id
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, $10, $11, $12)
                            ON CONFLICT (coalesce(deployment_id, -1), symbol, side, dry_run) WHERE status = 'OPEN' DO NOTHING
                            RETURNING id
                            """,
                            symbol,
                            side,
                            size,
                            avg_price,
                            'OPEN',
                            opened_at,
                            unrealised_pnl,
                            json.dumps(entry_meta),
                            initial_stop_price,
                            now,
                            now,
                            # Attributed from the opening order's orderLinkId (above): the owner if WE
                            # opened it, else NULL for a genuinely external position.
                            imp_dep,
                        )
                        
                        if position_row is None:
                            # ON CONFLICT: position already exists (created by execute_entry concurrently)
                            logger.info(
                                f"[PositionTracker] {symbol} {side}: Skipped import - "
                                "OPEN position already exists (concurrent execute_entry)"
                            )
                            continue

                        position_id = position_row['id']

                        # Create a corresponding trades entry for stop-loss tracking
                        # This allows stop-loss updates to be saved properly
                        entry_side = 'BUY' if side == 'LONG' else 'SELL'
                        
                        await conn.execute(
                            """
                            INSERT INTO trades (
                                position_id, symbol, trade_type, side, price, time,
                                qty, reason, created_at, updated_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                            """,
                            position_id,
                            symbol,
                            'import',
                            entry_side,
                            avg_price,
                            opened_at,
                            size,
                            IMPORT_TRADE_REASON,
                            now,
                            now,
                        )
                        if initial_stop_price is not None:
                            await conn.execute(
                                "UPDATE trades SET stop_loss_price = $1 WHERE position_id = $2",
                                initial_stop_price,
                                position_id,
                            )
                        
                        imported_symbols.append(symbol)
                        logger.info(
                            f"[PositionTracker] Imported {symbol} {side} "
                            f"size={size} @ ${avg_price:,.2f}"
                        )
                        
                    except Exception as e:
                        import traceback
                        symbol = bybit_pos.get('symbol', 'UNKNOWN')
                        logger.error(
                            f"[PositionTracker] CRITICAL: Failed to import {symbol}: {e}\n"
                            f"Position details: side={bybit_pos.get('side')}, size={bybit_pos.get('size')}, "
                            f"avgPrice={bybit_pos.get('avgPrice')}\n"
                            f"Traceback: {traceback.format_exc()}"
                        )
                        # Continue with next position instead of failing the entire import
                        continue
            
            logger.info(
                f"[PositionTracker] Successfully imported {len(imported_symbols)} positions: "
                f"{', '.join(imported_symbols)}"
            )
            
            return {
                'imported_count': len(imported_symbols),
                'total_exchange_positions': len(active_bybit_positions),
                'already_tracked': already_tracked,
                'imported_symbols': imported_symbols
            }
            
        except Exception as e:
            import traceback
            logger.error(
                f"[PositionTracker] CRITICAL: Error importing exchange positions: {e}\n"
                f"Traceback: {traceback.format_exc()}"
            )
            return {
                'imported_count': 0,
                'total_exchange_positions': 0,
                'already_tracked': 0,
                'imported_symbols': [],
                'error': str(e),
                'traceback': traceback.format_exc()
            }
