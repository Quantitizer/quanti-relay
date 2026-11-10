"""Regression tests for Bybit per-account API-key routing (several accounts in one relay).

A deployment with its own `credentials_ref` (e.g. a sub-account) must trade on ITS OWN keys,
resolved from env {REF}_API_KEY / {REF}_API_SECRET — NOT the default account's keys
(BYBIT_API_KEY / BYBIT_API_SECRET). `credentials_ref` None/'' means the default account.
A ref that is set but whose env vars are absent must raise (never silently mis-route to the
shared account).
"""
import pytest

import relay.exchange.bybit.client_factory as factory
from relay.exchange.bybit.client_factory import (
    get_bybit_client,
    resolve_bybit_credentials,
)

REF = "sub-account-1"


@pytest.fixture(autouse=True)
def _clear_client_cache():
    factory._CLIENT_CACHE.clear()
    yield
    factory._CLIENT_CACHE.clear()


def test_resolve_none_falls_back_to_global():
    assert resolve_bybit_credentials(None) == (None, None)
    assert resolve_bybit_credentials("") == (None, None)


def test_resolve_reads_env_keys(monkeypatch):
    monkeypatch.setenv("SUB_ACCOUNT_1_API_KEY", "hk_test")
    monkeypatch.setenv("SUB_ACCOUNT_1_API_SECRET", "hs_test")
    assert resolve_bybit_credentials(REF) == ("hk_test", "hs_test")


def test_resolve_uppercases_and_dashes(monkeypatch):
    monkeypatch.setenv("MY_ACCT_API_KEY", "k")
    monkeypatch.setenv("MY_ACCT_API_SECRET", "s")
    assert resolve_bybit_credentials("my-acct") == ("k", "s")


def test_resolve_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NO_SUCH_ACCT_API_KEY", raising=False)
    monkeypatch.delenv("NO_SUCH_ACCT_API_SECRET", raising=False)
    with pytest.raises(KeyError):
        resolve_bybit_credentials("no_such_acct")


def test_resolve_partial_env_raises(monkeypatch):
    # key present, secret absent -> still refuse (never a half-authenticated client)
    monkeypatch.setenv("HALF_ACCT_API_KEY", "k")
    monkeypatch.delenv("HALF_ACCT_API_SECRET", raising=False)
    with pytest.raises(KeyError):
        resolve_bybit_credentials("half_acct")


def test_default_client_uses_default_env_keys(monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "dk")
    monkeypatch.setenv("BYBIT_API_SECRET", "ds")
    c = get_bybit_client(demo=True, testnet=False)          # credentials_ref None
    assert (c.api_key, c.api_secret) == ("dk", "ds")
    assert c.base_url == "https://api-demo.bybit.com"       # demo=True


def test_keyed_client_is_distinct_and_uses_account_keys(monkeypatch):
    monkeypatch.setenv("SUB_ACCOUNT_1_API_KEY", "hk_test")
    monkeypatch.setenv("SUB_ACCOUNT_1_API_SECRET", "hs_test")
    default_c = get_bybit_client(demo=True, testnet=False)
    keyed_c = get_bybit_client(demo=False, testnet=False, credentials_ref=REF)
    assert keyed_c is not default_c
    assert (keyed_c.api_key, keyed_c.api_secret) == ("hk_test", "hs_test")
    # explicit demo=False/testnet=False -> mainnet endpoint, not the demo host
    assert keyed_c.base_url == "https://api.bybit.com"


def test_keyed_client_is_cached_per_ref(monkeypatch):
    monkeypatch.setenv("SUB_ACCOUNT_1_API_KEY", "hk_test")
    monkeypatch.setenv("SUB_ACCOUNT_1_API_SECRET", "hs_test")
    a = get_bybit_client(demo=False, testnet=False, credentials_ref=REF)
    b = get_bybit_client(demo=False, testnet=False, credentials_ref=REF)
    assert a is b   # shared time-sync / circuit-breaker state per account


def test_missing_keyed_env_raises_at_build(monkeypatch):
    monkeypatch.delenv("SUB_ACCOUNT_1_API_KEY", raising=False)
    monkeypatch.delenv("SUB_ACCOUNT_1_API_SECRET", raising=False)
    with pytest.raises(KeyError):
        get_bybit_client(demo=False, testnet=False, credentials_ref=REF)
