"""
Bybit Private WebSocket client for account data streams.

Provides real-time account updates:
- Position updates (size, PnL, liquidation)
- Order updates (fills, rejects, cancellations)
- Wallet updates (balance changes)

Requires API key/secret for authentication.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

try:
    import websockets
except ImportError:
    websockets = None

from relay.config import settings

logger = logging.getLogger(__name__)



def _num(value) -> float | None:
    """A numeric message field, or None when it is absent or empty (never a made-up 0)."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

class OrderStatus(Enum):
    """Bybit order status values."""
    NEW = "New"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    PENDING_CANCEL = "PendingCancel"
    PARTIALLY_FILLED_CANCELED = "PartiallyFilledCanceled"
    UNTRIGGERED = "Untriggered"     # conditional (stop/TP) order waiting for its trigger
    TRIGGERED = "Triggered"
    DEACTIVATED = "Deactivated"     # conditional order cancelled before triggering
    UNKNOWN = "Unknown"             # a status this client does not know (raw value in raw_status)


class PositionSide(Enum):
    """Position side."""
    LONG = "Buy"
    SHORT = "Sell"
    NONE = "None"


@dataclass
class PositionUpdate:
    """Represents a position update from WebSocket."""
    # Numeric fields are None when the message does not carry them: "absent" must never read as 0
    # (size 0 means CLOSED and closes the position's row).
    symbol: str
    side: PositionSide
    size: float | None
    entry_price: float | None
    mark_price: float | None
    unrealized_pnl: float | None
    realized_pnl: float
    leverage: float | None
    liq_price: float | None  # Liquidation price
    take_profit: float | None
    stop_loss: float | None
    timestamp: datetime
    
    @classmethod
    def from_bybit_data(cls, data: dict) -> "PositionUpdate":
        """Create PositionUpdate from Bybit WebSocket data."""
        side_str = data.get("side", "None")
        try:
            side = PositionSide(side_str)
        except ValueError:
            side = PositionSide.NONE
            
        return cls(
            symbol=data.get("symbol", ""),
            side=side,
            size=_num(data.get("size")),
            entry_price=_num(data.get("entryPrice")),
            mark_price=_num(data.get("markPrice")),
            unrealized_pnl=_num(data.get("unrealisedPnl")),
            realized_pnl=_num(data.get("cumRealisedPnl")),
            leverage=_num(data.get("leverage")),
            liq_price=_num(data.get("liqPrice")),
            take_profit=float(data.get("takeProfit", 0)) if data.get("takeProfit") else None,
            stop_loss=float(data.get("stopLoss", 0)) if data.get("stopLoss") else None,
            timestamp=datetime.fromtimestamp(int(data.get("updatedTime", 0)) / 1000, tz=UTC) if data.get("updatedTime") else datetime.now(UTC),
        )


@dataclass
class OrderUpdate:
    """Represents an order update from WebSocket."""
    order_id: str
    order_link_id: str  # Client order ID
    symbol: str
    side: str
    order_type: str
    price: float
    qty: float
    filled_qty: float
    avg_price: float
    status: OrderStatus
    reduce_only: bool
    created_at: datetime
    updated_at: datetime
    reject_reason: str | None
    raw_status: str = ""
    
    @classmethod
    def from_bybit_data(cls, data: dict) -> "OrderUpdate":
        """Create OrderUpdate from Bybit WebSocket data."""
        status_str = data.get("orderStatus", "")
        try:
            status = OrderStatus(status_str)
        except ValueError:
            status = OrderStatus.UNKNOWN
            
        return cls(
            order_id=data.get("orderId", ""),
            order_link_id=data.get("orderLinkId", ""),
            symbol=data.get("symbol", ""),
            side=data.get("side", ""),
            order_type=data.get("orderType", ""),
            price=float(data.get("price", 0) or 0),
            qty=float(data.get("qty", 0) or 0),
            filled_qty=float(data.get("cumExecQty", 0) or 0),
            avg_price=float(data.get("avgPrice", 0) or 0),
            status=status,
            reduce_only=data.get("reduceOnly", False),
            created_at=datetime.fromtimestamp(int(data.get("createdTime", 0)) / 1000, tz=UTC) if data.get("createdTime") else datetime.now(UTC),
            updated_at=datetime.fromtimestamp(int(data.get("updatedTime", 0)) / 1000, tz=UTC) if data.get("updatedTime") else datetime.now(UTC),
            # Bybit sends rejectReason "EC_NoError" on every normal order: that is not a rejection.
            reject_reason=None if data.get("rejectReason") in (None, "", "EC_NoError") else data.get("rejectReason"),
            raw_status=status_str,
        )


@dataclass
class WalletUpdate:
    """Represents a wallet/balance update from WebSocket."""
    coin: str
    equity: float
    available_balance: float
    wallet_balance: float
    unrealized_pnl: float
    timestamp: datetime
    
    @classmethod
    def from_bybit_data(cls, data: dict) -> "WalletUpdate":
        """Create WalletUpdate from Bybit WebSocket data."""
        return cls(
            coin=data.get("coin", ""),
            equity=float(data.get("equity", 0) or 0),
            available_balance=float(data.get("availableToWithdraw", 0) or 0),
            wallet_balance=float(data.get("walletBalance", 0) or 0),
            unrealized_pnl=float(data.get("unrealisedPnl", 0) or 0),
            timestamp=datetime.now(UTC),
        )


class BybitPrivateWsClient:
    """
    Bybit Private WebSocket client for account data.
    
    Features:
    - HMAC authentication
    - Real-time position tracking
    - Order fill/reject notifications
    - Wallet balance updates
    - Auto-reconnect with re-authentication
    
    Usage:
        client = BybitPrivateWsClient(api_key="...", api_secret="...")
        client.register_position_handler(on_position_update)
        client.register_order_handler(on_order_update)
        await client.start()
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
        demo: bool | None = None,
    ):
        """
        Initialize Bybit Private WebSocket client.

        Args:
            api_key: Bybit API key (defaults to env BYBIT_API_KEY)
            api_secret: Bybit API secret (defaults to env BYBIT_API_SECRET)
            testnet: Use testnet (defaults to settings.testnet)
            demo: Use demo trading (defaults to settings.bybit_demo). Demo private streams
                  must use stream-demo.bybit.com per Bybit docs.
        """
        self.api_key = api_key or os.environ.get("BYBIT_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("BYBIT_API_SECRET", "")
        self.testnet = testnet if testnet is not None else settings.testnet
        self.demo = demo if demo is not None else settings.bybit_demo

        # Validate credentials
        if not self.api_key or not self.api_secret:
            logger.warning("[Private WS] No API credentials provided - private streams will not work")

        # WebSocket URLs - Demo private streams must use stream-demo.bybit.com (Bybit docs).
        # Demo takes priority over testnet, matching the REST client: a demo key is unknown to testnet.
        if self.demo:
            self.ws_url = "wss://stream-demo.bybit.com/v5/private"
        elif self.testnet:
            self.ws_url = "wss://stream-testnet.bybit.com/v5/private"
        else:
            self.ws_url = "wss://stream.bybit.com/v5/private"
        
        self.connection = None
        self.is_running = False
        self.is_authenticated = False
        self.is_subscribed = False
        self._task: asyncio.Task | None = None
        self._reconnect_attempt = 0  # Exponential backoff: 5, 10, 20, 40, cap 60
        
        # Callbacks
        self.position_callbacks: list[Callable[[PositionUpdate], Any]] = []
        self.order_callbacks: list[Callable[[OrderUpdate], Any]] = []
        self.wallet_callbacks: list[Callable[[WalletUpdate], Any]] = []
        
        # Track last known positions for change detection
        self.last_positions: dict[str, PositionUpdate] = {}
        
        # Heartbeat health tracking
        self._last_ping_ts: float = 0.0
        self._last_pong_ts: float = 0.0
        self._connection_started_ts: float = 0.0
    
    async def start(self):
        """Start the WebSocket connection."""
        if not self.api_key or not self.api_secret:
            logger.error("[Private WS] Cannot start without API credentials")
            return
        
        if self.is_running:
            logger.warning("[Private WS] Already running")
            return
        
        self.is_running = True
        logger.info("[Private WS] Starting private WebSocket listener")
        
        # The connection loop runs in the background; kept so stop() can end it (a second start()
        # while it runs is refused above, so there is never more than one connection).
        self._task = asyncio.create_task(self._maintain_connection())

    async def stop(self):
        """Stop the WebSocket connection and its reconnect loop."""
        self.is_running = False
        self.is_authenticated = False
        self.is_subscribed = False
        task, self._task = getattr(self, "_task", None), None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        
        if self.connection:
            try:
                await self.connection.close()
                logger.info("[Private WS] Connection closed gracefully")
            except Exception as e:
                logger.error(f"[Private WS] Error closing connection: {e}")
        
        self.connection = None
        logger.info("[Private WS] Stopped")
    
    async def _maintain_connection(self):
        """
        Maintain WebSocket connection with auto-reconnect.

        Includes authentication on each connect.
        Protocol-level ping: enabled when bybit_ws_ping_interval > 0.
        App-level heartbeat {"op": "ping"} always runs as fallback.
        """
        pi = getattr(settings, "bybit_ws_ping_interval", 20)
        ping_interval = pi if pi and pi > 0 else None
        ping_timeout = getattr(settings, "bybit_ws_ping_timeout", 45) if ping_interval else None
        if ping_interval:
            logger.info(f"[Private WS] Protocol ping: interval={ping_interval}s, timeout={ping_timeout}s")
        while self.is_running:
            try:
                logger.info(f"[Private WS] Connecting to {self.ws_url}")

                async with websockets.connect(
                    self.ws_url,
                    ping_interval=ping_interval,
                    ping_timeout=ping_timeout,
                    close_timeout=5
                ) as ws:
                    self.connection = ws
                    self._last_pong_ts = time.time()
                    self._last_ping_ts = 0.0
                    self._connection_started_ts = time.time()
                    logger.info("[Private WS] Connected")
                    
                    # Authenticate
                    if not await self._authenticate():
                        logger.error("[Private WS] Authentication failed, reconnecting...")
                        delay = min(5 * (2 ** self._reconnect_attempt), 60)
                        self._reconnect_attempt += 1
                        await asyncio.sleep(delay)
                        continue
                    # (the back-off resets only once a subscription is confirmed, see _handle_message)

                    # Start Bybit heartbeat task
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    
                    # Subscribe to private streams
                    self.is_subscribed = False   # until the venue confirms the subscription
                    await self._subscribe_private_streams()
                    
                    try:
                        # Message loop
                        async for message in ws:
                            if not self.is_running:
                                break
                            await self._handle_message(message)
                    finally:
                        # Cancel heartbeat when connection ends
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass

                # The connection ended without an error (closed normally - by us after a refused
                # subscription, or by the venue): back off before reconnecting, never a tight loop.
                self.is_authenticated = False
                self.is_subscribed = False
                if self.is_running:
                    delay = min(5 * (2 ** self._reconnect_attempt), 60)
                    self._reconnect_attempt += 1
                    logger.warning(f"[Private WS] Connection ended. Reconnecting in {delay}s...")
                    await asyncio.sleep(delay)

            except websockets.exceptions.ConnectionClosed as e:
                age = time.time() - self._connection_started_ts if self._connection_started_ts else 0
                delay = min(5 * (2 ** self._reconnect_attempt), 60)
                self._reconnect_attempt += 1
                logger.warning(f"[Private WS] Connection closed after {age:.0f}s: {e}. Reconnecting in {delay}s...")
                self.is_authenticated = False
                self.is_subscribed = False
                await asyncio.sleep(delay)
                
            except Exception as e:
                age = time.time() - self._connection_started_ts if self._connection_started_ts else 0
                delay = min(5 * (2 ** self._reconnect_attempt), 60)
                self._reconnect_attempt += 1
                logger.error(f"[Private WS] Error after {age:.0f}s: {e}. Reconnecting in {delay}s...")
                self.is_authenticated = False
                self.is_subscribed = False
                await asyncio.sleep(delay)
    
    async def _heartbeat_loop(self, ws):
        """
        Send Bybit application-level heartbeats every 20 seconds.
        
        Bybit requires {"op": "ping"} messages to keep the connection alive.
        The server replies {"op": "pong"} (see _handle_message).
        
        If the loop encounters an error or detects a missing pong, it
        force-closes the WS so _maintain_connection reconnects immediately.
        """
        PONG_TIMEOUT = getattr(settings, "bybit_ws_pong_timeout", 45)
        while self.is_running:
            try:
                await asyncio.sleep(20)
                if self._last_ping_ts > 0 and self._last_pong_ts > 0:
                    pong_age = time.time() - self._last_pong_ts
                    if pong_age > PONG_TIMEOUT:
                        logger.warning(
                            f"[Private WS] No pong received for {pong_age:.0f}s "
                            f"(timeout={PONG_TIMEOUT}s), closing connection"
                        )
                        break
                if ws.open:
                    await ws.send(json.dumps({"op": "ping"}))
                    self._last_ping_ts = time.time()
                    logger.debug("[Private WS] Sent heartbeat ping")
                else:
                    logger.warning("[Private WS] Heartbeat: ws not open, closing to trigger reconnect")
                    break
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"[Private WS] Heartbeat error: {e}, closing connection to trigger reconnect")
                break
        if ws.open:
            try:
                await ws.close()
            except Exception as e:
                logger.debug(f"[Private WS] closing a failing connection: {e}")
    
    async def _authenticate(self) -> bool:
        """
        Authenticate the WebSocket connection using HMAC.
        
        Returns:
            True if authentication successful, False otherwise
        """
        try:
            # Generate signature
            # Valid for 30s: a local clock a little behind the venue's still authenticates.
            expires = int((time.time() + 30) * 1000)
            val = f"GET/realtime{expires}"
            
            signature = hmac.new(
                self.api_secret.encode("utf-8"),
                val.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            
            # Send auth message
            auth_message = {
                "op": "auth",
                "args": [self.api_key, expires, signature]
            }
            
            await self.connection.send(json.dumps(auth_message))
            logger.info("[Private WS] Authentication request sent")
            
            # Wait for auth response (with timeout)
            try:
                response = await asyncio.wait_for(
                    self.connection.recv(),
                    timeout=10.0
                )
                data = json.loads(response)
                
                if data.get("success"):
                    self.is_authenticated = True
                    logger.info("[Private WS] ✓ Authenticated successfully")
                    return True
                else:
                    logger.error(f"[Private WS] ✗ Auth failed: {data.get('ret_msg', 'Unknown error')}")
                    return False
                    
            except TimeoutError:
                logger.error("[Private WS] Auth response timeout")
                return False
                
        except Exception as e:
            logger.error(f"[Private WS] Auth error: {e}")
            return False
    
    async def _subscribe_private_streams(self):
        """Subscribe to private account streams."""
        if not self.is_authenticated:
            logger.warning("[Private WS] Cannot subscribe - not authenticated")
            return
        
        # Subscribe to position, order, and wallet streams
        subscribe_message = {
            "op": "subscribe",
            "args": [
                "position",  # Position updates
                "order",     # Order updates
                "wallet",    # Wallet/balance updates
            ]
        }
        
        try:
            await self.connection.send(json.dumps(subscribe_message))
            logger.info("[Private WS] ✓ Subscribed to position, order, wallet streams")
        except Exception as e:
            logger.error(f"[Private WS] Error subscribing to private streams: {e}")
    
    async def _handle_message(self, raw_message: str):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(raw_message)

            # Heartbeat reply. Checked first: Bybit's replies carry an "op" key (private stream:
            # {"op": "pong"}; public: {"op": "ping", "ret_msg": "pong"}), so the operation-response
            # branch below would swallow them and the connection would be recycled every ~60s.
            if data.get("op") == "pong" or data.get("ret_msg") == "pong":
                self._last_pong_ts = time.time()
                return

            # Handle different message types
            if "topic" in data:
                topic = data["topic"]
                
                if topic == "position":
                    await self._handle_position_update(data)
                elif topic == "order":
                    await self._handle_order_update(data)
                elif topic == "wallet":
                    await self._handle_wallet_update(data)
            
            elif "op" in data:
                # Operation responses
                op = data.get("op")
                success = data.get("success", False)
                
                if op == "subscribe":
                    if success:
                        self.is_subscribed = True
                        self._reconnect_attempt = 0   # a fully working connection resets the back-off
                        logger.info("[Private WS] ✓ Subscription confirmed")
                    else:
                        # Authenticated but deaf: no position/order data would arrive. Drop the
                        # connection so the reconnect loop subscribes again (with back-off).
                        self.is_subscribed = False
                        logger.error(f"[Private WS] ✗ Subscription failed: {data.get('ret_msg')} - reconnecting")
                        if self.connection is not None:
                            await self.connection.close()
                
        except json.JSONDecodeError as e:
            logger.error(f"[Private WS] JSON parse error: {e}")
        except Exception as e:
            logger.error(f"[Private WS] Message handling error: {e}")
    
    async def _handle_position_update(self, data: dict):
        """Process position update message."""
        try:
            positions = data.get("data", [])
            
            for pos_data in positions:
                position = PositionUpdate.from_bybit_data(pos_data)
                
                logger.info(
                    f"[Private WS] Position Update: {position.symbol} "
                    f"{position.side.value} Size={position.size} "
                    f"Entry={position.entry_price} "
                    f"PnL={position.unrealized_pnl}"
                )
                
                # Store for tracking
                self.last_positions[position.symbol] = position
                
                # Call registered callbacks
                for callback in self.position_callbacks:
                    try:
                        await callback(position)
                    except Exception as e:
                        logger.error(f"[Private WS] Error in position callback: {e}")
                        
        except Exception as e:
            logger.error(f"[Private WS] Error processing position update: {e}")
    
    async def _handle_order_update(self, data: dict):
        """Process order update message."""
        try:
            orders = data.get("data", [])
            
            for order_data in orders:
                order = OrderUpdate.from_bybit_data(order_data)
                
                status_emoji = {
                    OrderStatus.FILLED: "✓",
                    OrderStatus.REJECTED: "✗",
                    OrderStatus.CANCELLED: "⊘",
                    OrderStatus.PARTIALLY_FILLED: "◐",
                }.get(order.status, "○")
                
                logger.info(
                    f"[Private WS] Order Update: {status_emoji} {order.symbol} "
                    f"{order.side} {order.order_type} "
                    f"Qty={order.qty} Filled={order.filled_qty} "
                    f"Status={order.status.value}"
                )
                
                if order.reject_reason:
                    logger.warning(f"[Private WS] Order rejected: {order.reject_reason}")
                
                # Call registered callbacks
                for callback in self.order_callbacks:
                    try:
                        await callback(order)
                    except Exception as e:
                        logger.error(f"[Private WS] Error in order callback: {e}")
                        
        except Exception as e:
            logger.error(f"[Private WS] Error processing order update: {e}")
    
    async def _handle_wallet_update(self, data: dict):
        """Process wallet/balance update message."""
        try:
            wallets = data.get("data", [])
            
            for wallet_data in wallets:
                # Wallet data contains a list of coins
                coins = wallet_data.get("coin", [])
                
                for coin_data in coins:
                    wallet = WalletUpdate.from_bybit_data(coin_data)
                    
                    logger.debug(
                        f"[Private WS] Wallet Update: {wallet.coin} "
                        f"Balance={wallet.wallet_balance:.4f} "
                        f"Available={wallet.available_balance:.4f}"
                    )
                    
                    # Call registered callbacks
                    for callback in self.wallet_callbacks:
                        try:
                            await callback(wallet)
                        except Exception as e:
                            logger.error(f"[Private WS] Error in wallet callback: {e}")
                            
        except Exception as e:
            logger.error(f"[Private WS] Error processing wallet update: {e}")
    
    def register_position_handler(self, callback: Callable[[PositionUpdate], Any]):
        """
        Register a callback for position updates.
        
        Args:
            callback: Async function that receives PositionUpdate
        """
        if callback not in self.position_callbacks:
            self.position_callbacks.append(callback)
            logger.info(f"[Private WS] Registered position handler: {callback.__name__}")
    
    def register_order_handler(self, callback: Callable[[OrderUpdate], Any]):
        """
        Register a callback for order updates.
        
        Args:
            callback: Async function that receives OrderUpdate
        """
        if callback not in self.order_callbacks:
            self.order_callbacks.append(callback)
            logger.info(f"[Private WS] Registered order handler: {callback.__name__}")
    
    def register_wallet_handler(self, callback: Callable[[WalletUpdate], Any]):
        """
        Register a callback for wallet updates.
        
        Args:
            callback: Async function that receives WalletUpdate
        """
        if callback not in self.wallet_callbacks:
            self.wallet_callbacks.append(callback)
            logger.info(f"[Private WS] Registered wallet handler: {callback.__name__}")
    
    def unregister_position_handler(self, callback: Callable):
        """Remove a position callback handler."""
        if callback in self.position_callbacks:
            self.position_callbacks.remove(callback)
    
    def unregister_order_handler(self, callback: Callable):
        """Remove an order callback handler."""
        if callback in self.order_callbacks:
            self.order_callbacks.remove(callback)
    
    def unregister_wallet_handler(self, callback: Callable):
        """Remove a wallet callback handler."""
        if callback in self.wallet_callbacks:
            self.wallet_callbacks.remove(callback)
    
    def get_position(self, symbol: str) -> PositionUpdate | None:
        """
        Get last known position for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Last PositionUpdate or None if no position
        """
        return self.last_positions.get(symbol)
    
    def get_all_positions(self) -> dict[str, PositionUpdate]:
        """Get all tracked positions."""
        return self.last_positions.copy()
    
    def get_pong_age(self) -> float:
        """Seconds since last pong was received (0 if no pong yet)."""
        if self._last_pong_ts == 0:
            return 0.0
        return time.time() - self._last_pong_ts
    
    def is_healthy(self) -> bool:
        """True if the WS connection is alive, authenticated, subscribed, and pongs are recent."""
        if not self.is_running or not self.connection or not self.is_authenticated or not self.is_subscribed:
            return False
        return self.get_pong_age() < 60
    
    def get_connection_age(self) -> float:
        """Seconds since the current WS session started."""
        if self._connection_started_ts == 0:
            return 0.0
        return time.time() - self._connection_started_ts
    
    def __repr__(self) -> str:
        return (
            f"<BybitPrivateWsClient("
            f"testnet={self.testnet}, "
            f"authenticated={self.is_authenticated}, "
            f"running={self.is_running}, "
            f"positions={len(self.last_positions)}"
            f")>"
        )
