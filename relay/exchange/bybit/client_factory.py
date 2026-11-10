"""
Bybit REST client factory - returns cached singleton instances per (demo, testnet, credentials_ref).
Ensures shared time sync / circuit-breaker state across all callers of the SAME account.

Per-account keys (multi-account routing): a deployment's `credentials_ref` selects a distinct keyed
client. The ref is a key-id into the process env — NOT a plaintext key.
Convention: ref 'sub-account-1' -> env SUB_ACCOUNT_1_API_KEY / SUB_ACCOUNT_1_API_SECRET
(ref upper-cased, '-' -> '_'). ref None/'' -> the default account (env BYBIT_API_KEY /
BYBIT_API_SECRET). A ref that is set but whose env vars are absent raises KeyError —
callers must decide (live => refuse to trade the shared account; dry-run => degrade) rather than
silently falling back to the wrong account's keys.
"""
import os

from relay.config import settings
from relay.exchange.bybit.rest_client import BybitRestClient

_CLIENT_CACHE: dict[tuple[bool, bool, str | None], BybitRestClient] = {}


def resolve_bybit_credentials(credentials_ref: str | None) -> tuple[str | None, str | None]:
    """Map a `credentials_ref` to (api_key, api_secret) from the process env.

    None/'' -> (None, None): the client falls back to BYBIT_API_KEY / BYBIT_API_SECRET (default account).
    A set ref -> reads {REF}_API_KEY / {REF}_API_SECRET (upper-cased, '-'->'_'); raises KeyError if
    either is missing (never silently fall back to the wrong account)."""
    if not credentials_ref:
        return None, None
    base = credentials_ref.upper().replace("-", "_")
    api_key = os.environ.get(f"{base}_API_KEY")
    api_secret = os.environ.get(f"{base}_API_SECRET")
    if not api_key or not api_secret:
        raise KeyError(
            f"credentials_ref '{credentials_ref}': env {base}_API_KEY / {base}_API_SECRET not set")
    return api_key, api_secret


def get_bybit_client(
    demo: bool | None = None,
    testnet: bool | None = None,
    credentials_ref: str | None = None,
) -> BybitRestClient:
    """
    Get a BybitRestClient instance, cached by (demo, testnet, credentials_ref).
    When demo/testnet are None, uses settings.bybit_demo / settings.testnet.
    When credentials_ref is None, uses the default account's keys (BYBIT_API_KEY / BYBIT_API_SECRET);
    otherwise the account-specific keys resolved from env (see module docstring / resolve_bybit_credentials).
    """
    demo_val = demo if demo is not None else settings.bybit_demo
    testnet_val = testnet if testnet is not None else settings.testnet
    key = (demo_val, testnet_val, credentials_ref or None)
    cached = _CLIENT_CACHE.get(key)
    # Defensive self-heal: never hand back a client whose httpx session was closed. If some code path
    # closes the shared singleton (e.g. a component closing its adapter on shutdown), the next
    # get_bybit_client() would otherwise keep returning the dead client forever (every request raises
    # 'Cannot send a request, as the client has been closed' -> the bybit_trading circuit breaker opens,
    # and only a process restart recovers). Detect the closed session and rebuild in place.
    if cached is not None and getattr(getattr(cached, "client", None), "is_closed", False):
        import logging
        logging.getLogger(__name__).warning(
            "[bybit_factory] cached client %s had a closed httpx session — rebuilding", key)
        cached = None
    if cached is None:
        api_key, api_secret = resolve_bybit_credentials(credentials_ref)
        cached = BybitRestClient(
            api_key=api_key, api_secret=api_secret, demo=demo_val, testnet=testnet_val)
        _CLIENT_CACHE[key] = cached
    return cached
