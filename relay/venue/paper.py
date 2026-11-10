"""Paper trading without exchange keys: the venue answers prices and market rules, but has no
account to read. Sizing and the risk checks need an equity, so the owner gives one
(`paper_equity` in the deployment file, in USDT). Empty means entries are refused, saying why: the
relay presets no number.
"""
from __future__ import annotations

from typing import Any

NO_EQUITY = ("no exchange keys, so no balance to read: set paper_equity in the deployment (the USDT "
             "to pretend the account holds) to trade on paper without keys")


class PaperAccount:
    """The venue client, with the balance taken from paper_equity. Everything else is the venue's."""

    def __init__(self, client: Any, equity: float | None):
        self._client = client
        self._equity = equity

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def get_balance_safe(self, coin: str = "USDT") -> tuple[float, str]:
        if self._equity is None:
            return 0.0, NO_EQUITY
        return float(self._equity), "paper_equity"

    async def get_usdt_trading_balance(self) -> tuple[float, str]:
        return await self.get_balance_safe()
