import asyncio
import types

import pytest

import relay.exchange.bybit.client_factory as factory
from relay.venue.adapter import BybitVenueAdapter, get_venue_adapter


class _Client:
    closed = False

    async def get_positions(self, category="linear", symbol=None):
        return [{"category": category, "symbol": symbol}]

    async def close(self):
        self.closed = True


def test_contract_mode_is_injected_as_default_category():
    a = BybitVenueAdapter(_Client(), contract_mode="inverse")
    assert asyncio.run(a.get_positions(symbol="BTCUSD")) == [{"category": "inverse", "symbol": "BTCUSD"}]
    assert asyncio.run(a.get_positions(category="linear")) == [{"category": "linear", "symbol": None}]


def test_close_never_closes_the_shared_client():
    c = _Client()
    asyncio.run(BybitVenueAdapter(c).close())
    assert c.closed is False


def test_pair_key():
    assert BybitVenueAdapter(_Client()).pair_key("BTCUSDT") == "bybit:BTCUSDT"


def test_factory_routes_bybit_and_rejects_unknown_venue(monkeypatch):
    factory._CLIENT_CACHE.clear()
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    a = get_venue_adapter(types.SimpleNamespace(venue="bybit", demo=False, testnet=True, credentials_ref=None))
    assert a.base_url == "https://api-testnet.bybit.com"
    with pytest.raises(NotImplementedError):
        get_venue_adapter(types.SimpleNamespace(venue="nowhere"))
    factory._CLIENT_CACHE.clear()
