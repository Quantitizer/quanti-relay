"""NOT WIRED YET: the running relay does not use this module. It is kept for the venue-interface phase (multi-venue), and reviewed and fixed when it is connected (tests/test_unwired_modules.py keeps the running relay from importing it before then).

VenueAdapter — the venue-agnostic execution seam.

Design constraints:
- Callers read RAW exchange response fields (position `size`/`side`/`stopLoss`, ticker
  `bid1Price`, instrument `lotSizeFilter`). So the adapter is a PASS-THROUGH: it forwards return
  values unchanged. Normalizing responses is the job of the plugin contract (SPEC §6), not of this
  seam.
- `category='linear'` is the one venue assumption worth owning here: the adapter injects its
  `contract_mode` as the default `category` on every category-bearing call, so a pool that trades
  inverse/spot flows through without the caller hardcoding it.
- The Bybit client is a per-`(demo,testnet)` singleton holding shared time-sync + circuit-breaker
  state; `get_venue_adapter` preserves that singleton via `get_bybit_client`.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from relay.util.symbols import construct_pair_id

# Client methods that accept a Bybit `category` kwarg — the adapter injects its contract_mode as the
# default for these (a caller may still override by passing `category=` explicitly).
_CATEGORY_METHODS = frozenset({
    "place_market_order", "place_limit_order", "amend_order", "cancel_order", "cancel_all_orders",
    "set_trading_stop", "set_leverage", "switch_isolated_margin",
    "get_positions", "get_open_orders", "get_order", "get_order_history", "get_closed_pnl",
    "get_ticker", "get_all_tickers", "list_instruments",
})


@runtime_checkable
class VenueAdapter(Protocol):
    """The venue-agnostic surface the execution path uses. Documents the intended contract; the
    concrete Bybit adapter forwards to the underlying client (preserving raw return shapes)."""
    venue_id: str
    contract_mode: str

    def pair_key(self, symbol: str) -> str: ...

    # order / position writes
    async def place_market_order(self, symbol: str, side: str, qty: str, **kwargs) -> dict: ...
    async def place_limit_order(self, symbol: str, side: str, qty: str, price: str, **kwargs) -> dict: ...
    async def amend_order(self, symbol: str, **kwargs) -> dict: ...
    async def cancel_order(self, symbol: str, **kwargs) -> dict: ...
    async def cancel_all_orders(self, **kwargs) -> dict: ...
    async def set_trading_stop(self, symbol: str, **kwargs) -> dict: ...
    async def set_leverage(self, symbol: str, leverage: int, **kwargs) -> dict: ...
    async def switch_isolated_margin(self, symbol: str, leverage: int, **kwargs) -> dict: ...
    # reads
    async def get_positions(self, **kwargs) -> list: ...
    async def get_open_orders(self, **kwargs) -> list: ...
    async def get_order(self, symbol: str, **kwargs) -> dict | None: ...
    async def get_order_history(self, **kwargs) -> list: ...
    async def get_closed_pnl(self, **kwargs) -> list: ...
    async def get_ticker(self, symbol: str, **kwargs) -> dict: ...
    async def get_all_tickers(self, **kwargs) -> dict: ...
    async def list_instruments(self, **kwargs) -> list: ...
    # balances / infra
    async def get_account_balance(self, **kwargs) -> float: ...
    async def get_balance_safe(self, coin: str = "USDT") -> tuple: ...
    async def get_usdt_trading_balance(self) -> tuple: ...
    def get_circuit_breaker_status(self) -> dict: ...


class BybitVenueAdapter:
    """Thin pass-through over `BybitRestClient`. Forwards every method 1:1, injecting `contract_mode`
    as the default `category` for category-bearing calls. Raw return dicts are preserved."""

    venue_id = "bybit"

    def __init__(self, client, contract_mode: str = "linear", venue_account=None):
        self._client = client
        self.contract_mode = contract_mode
        self.venue_account = venue_account   # optional back-reference

    def pair_key(self, symbol: str) -> str:
        """Venue-prefixed key, e.g. 'bybit:BTCUSDT' (replaces the inlined f'bybit:{symbol}')."""
        return construct_pair_id(self.venue_id, symbol)

    async def close(self) -> None:
        """No-op. The wrapped `BybitRestClient` is a shared per-(demo,testnet) SINGLETON owned by
        `client_factory` — its httpx session backs every Bybit call for that account. Closing it here (as `__getattr__` would, by forwarding `close` to the client) shuts
        that shared session, so every later request raises 'Cannot send a request, as the client has
        been closed' and the 'bybit_trading' circuit breaker opens. Per-request adapters must NEVER
        close the singleton; this explicit no-op overrides the forward. (HL adapters own a per-run
        session and DO implement a real close.)"""
        return

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not defined on the adapter itself → forward to the wrapped client.
        # (`self._client` / `self.contract_mode` are real instance attrs, so no recursion here.)
        attr = getattr(self._client, name)
        if name in _CATEGORY_METHODS and callable(attr):
            contract_mode = self.contract_mode

            async def _forward(*args, category: str | None = None, **kwargs):
                return await attr(*args, category=(category or contract_mode), **kwargs)

            _forward.__name__ = name
            return _forward
        return attr


def get_venue_adapter(venue_account, contract_mode: str = "linear") -> VenueAdapter:
    """Build a VenueAdapter for a `VenueAccount`. Preserves the per-`(demo,testnet)` Bybit client
    singleton (shared time-sync / circuit-breaker state). `contract_mode` comes from the pool.

    `venue_account.credentials_ref is None` ⇒ the underlying `get_bybit_client` uses the default
    account's keys (BYBIT_API_KEY / BYBIT_API_SECRET). A non-null `credentials_ref` (e.g. a
    sub-account) routes to THAT account's env keys — raising KeyError if they're absent (never silently the default
    account). Non-bybit venues raise until their adapter is implemented."""
    venue = getattr(venue_account, "venue", "bybit")
    if venue == "hyperliquid":
        from relay.venue.hyperliquid_adapter import build_hyperliquid_adapter
        return build_hyperliquid_adapter(venue_account, contract_mode=contract_mode)
    if venue != "bybit":
        raise NotImplementedError(f"No VenueAdapter for venue '{venue}' yet.")
    from relay.exchange.bybit.client_factory import get_bybit_client
    client = get_bybit_client(demo=getattr(venue_account, "demo", None),
                              testnet=getattr(venue_account, "testnet", None),
                              credentials_ref=getattr(venue_account, "credentials_ref", None))
    return BybitVenueAdapter(client, contract_mode=contract_mode, venue_account=venue_account)
