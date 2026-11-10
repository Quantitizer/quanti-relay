"""What an exchange API key may do, read from the exchange itself: can it trade, can it withdraw,
is it locked to addresses (and which).

The relay only ever needs a key that trades. A key that can withdraw puts the account's money one
leaked secret away from gone; a key locked to the server's address is useless to anyone who steals
it. Each exchange answers this in its own call and shape, read here; an exchange without a reader,
or one whose answer cannot be read, gives "unknown" (None), never a guess.

  Bybit (native and CCXT)  GET /v5/user/query-api: readOnly, permissions{group: [names]}, ips
  Binance                  GET sapi account/apiRestrictions: enable* flags, ipRestrict (no list)
  OKX                      GET account/config: perm "read_only,trade,withdraw", ip "a,b"
  KuCoin futures           GET user/api-key: permission "General,Futures,...", ipWhitelist "a,b"
  Hyperliquid              the private key's own address: the account's main wallet can withdraw, an
                           agent wallet (info "userRole": agent of the account) cannot
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class KeyCheck:
    exchange: str
    trade: bool | None = None
    withdraw: bool | None = None
    ip_bound: bool | None = None
    ips: list[str] | None = None            # None: the exchange does not say which
    error: str | None = None

    def verdict(self, server_ip: str | None = None) -> list[dict[str, Any]]:
        """[{check, status (ok | warn | fail), detail}] for this key, against the server's address
        when it is known."""
        out = []
        if self.error:
            out.append({"check": "key_readable", "status": "fail",
                        "detail": f"the exchange did not answer what the key may do: {self.error}"})
        out.append(_tri("key_trades", self.trade, ok="the key can trade",
                        bad="the key cannot trade: give it trade permission for this market",
                        unknown="whether the key can trade is not known: check it on the exchange", bad_is="fail"))
        out.append(_tri("key_no_withdrawal", None if self.withdraw is None else not self.withdraw,
                        ok="the key cannot withdraw",
                        bad="the key CAN WITHDRAW: delete it and make a new one without withdrawal permission",
                        unknown="whether the key can withdraw is not known: check on the exchange that it cannot",
                        bad_is="fail"))
        if self.ip_bound is False:
            where = f" to {server_ip}" if server_ip else " to the server's address"
            out.append({"check": "key_ip_bound", "status": "warn",
                        "detail": f"the key works from any address: lock it{where} on the exchange"})
        elif self.ip_bound and server_ip and self.ips is not None and server_ip not in self.ips:
            out.append({"check": "key_ip_bound", "status": "fail",
                        "detail": f"the key is locked to {', '.join(self.ips)}, not to this server ({server_ip}): "
                                  f"its calls from here will be refused"})
        elif self.ip_bound:
            out.append({"check": "key_ip_bound", "status": "ok",
                        "detail": "the key is locked to " + (", ".join(self.ips) if self.ips else "listed addresses")})
        else:
            out.append({"check": "key_ip_bound", "status": "warn",
                        "detail": "whether the key is locked to addresses is not known: check it on the exchange"})
        return out

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tri(check: str, value: bool | None, *, ok: str, bad: str, unknown: str, bad_is: str) -> dict[str, Any]:
    if value is True:
        return {"check": check, "status": "ok", "detail": ok}
    if value is False:
        return {"check": check, "status": bad_is, "detail": bad}
    return {"check": check, "status": "warn", "detail": unknown}


def _addresses(text: Any) -> list[str]:
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


# ---- the exchanges' answers ---------------------------------------------------------------------

def read_bybit(result: dict, market: str = "perpetual") -> KeyCheck:
    """`result` of GET /v5/user/query-api."""
    k = KeyCheck("bybit")
    perms = result.get("permissions")
    read_only = result.get("readOnly")
    if not isinstance(perms, dict) or read_only not in (0, 1, "0", "1"):
        k.error = "unexpected answer"
        return k
    names = {g: {str(n) for n in (v or [])} for g, v in perms.items() if isinstance(v, list)}
    trades = ("SpotTrade" in names.get("Spot", set()) if market == "spot" else
              "Order" in names.get("ContractTrade", set()) or "DerivativesTrade" in names.get("Derivatives", set()))
    k.trade = int(read_only) == 0 and trades
    k.withdraw = "Withdraw" in names.get("Wallet", set())
    ips = _addresses(result.get("ips"))
    k.ip_bound = bool(ips) and "*" not in ips
    k.ips = ips if k.ip_bound else []
    return k


def read_binance(r: dict, market: str = "perpetual") -> KeyCheck:
    """GET sapi account/apiRestrictions."""
    k = KeyCheck("binance")
    flag = "enableSpotAndMarginTrading" if market == "spot" else "enableFutures"
    if not isinstance(r.get(flag), bool) or not isinstance(r.get("enableWithdrawals"), bool):
        k.error = "unexpected answer"
        return k
    k.trade = r[flag]
    k.withdraw = r["enableWithdrawals"]
    k.ip_bound = r.get("ipRestrict") if isinstance(r.get("ipRestrict"), bool) else None
    return k                                    # which addresses: Binance no longer lists them here


def read_okx(r: dict, market: str = "perpetual") -> KeyCheck:
    """GET account/config."""
    k = KeyCheck("okx")
    data = r.get("data")
    row = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
    if row is None or not isinstance(row.get("perm"), str):
        k.error = "unexpected answer"
        return k
    perms = set(_addresses(row["perm"]))
    k.trade = "trade" in perms
    k.withdraw = "withdraw" in perms
    k.ips = _addresses(row.get("ip"))
    k.ip_bound = bool(k.ips)
    return k


# KuCoin's permission names that cannot move money off the exchange. Its documents name the one
# that can both "Withdrawal" and "Transfer"; a name not listed here is "unknown", never "cannot".
KUCOIN_NO_WITHDRAWAL = {"general", "spot", "margin", "futures", "earn", "innertransfer", "flextransfers",
                        "unified", "leadtradefutures", "leveragedtoken"}


def read_kucoin(r: dict, market: str = "perpetual") -> KeyCheck:
    """GET user/api-key."""
    k = KeyCheck("kucoinfutures")
    data = r.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("permission"), str):
        k.error = "unexpected answer"
        return k
    perms = {p.lower() for p in _addresses(data["permission"])}
    k.trade = ("spot" if market == "spot" else "futures") in perms
    if any("withdraw" in p or p == "transfer" for p in perms):
        k.withdraw = True
    elif perms <= KUCOIN_NO_WITHDRAWAL:
        k.withdraw = False
    if "ipWhitelist" in data:                    # not in every answer: then it is not known
        k.ips = _addresses(data.get("ipWhitelist"))
        k.ip_bound = bool(k.ips)
    return k


def eth_address(private_key: str) -> str | None:
    """The 0x address a secp256k1 private key signs as (lower case), or None when it cannot be worked out."""
    try:
        from ccxt.static_dependencies.keccak import SHA3
        from coincurve import PrivateKey
        public = PrivateKey(bytes.fromhex(private_key.removeprefix("0x"))).public_key.format(compressed=False)[1:]
        return "0x" + SHA3(public).hex()[-40:]
    except Exception:
        return None


async def read_hyperliquid(x) -> KeyCheck:
    """Whether the private key is the account's own wallet (it can withdraw) or an agent wallet the
    account approved (it trades, and Hyperliquid lets it withdraw nothing)."""
    k = KeyCheck("hyperliquid")
    account = str(getattr(x, "walletAddress", "") or "").lower()
    signer = eth_address(str(getattr(x, "privateKey", "") or ""))
    if not account or signer is None:
        k.error = "the wallet address or the private key cannot be read"
        return k
    if signer == account:
        k.trade, k.withdraw = True, True             # the main wallet's own key
        return k
    try:
        role = await x.publicPostInfo({"type": "userRole", "user": signer})
    except Exception as e:
        k.error = _short(e)
        return k
    master = str(((role or {}).get("data") or {}).get("user") or "").lower() if isinstance(role, dict) else ""
    if isinstance(role, dict) and role.get("role") == "agent" and master == account:
        k.trade, k.withdraw = True, False
    else:
        k.error = (f"the private key is not an agent wallet of {account} (Hyperliquid says: "
                   f"{role.get('role') if isinstance(role, dict) else role!r})")
    return k


# exchange -> (the CCXT call, the reader)
READERS = {
    "bybit": ("privateGetV5UserQueryApi", lambda r, m: read_bybit(r.get("result") or {}, m)),
    "binance": ("sapiGetAccountApiRestrictions", read_binance),
    "okx": ("privateGetAccountConfig", read_okx),
    "kucoinfutures": ("privateGetUserApiKey", read_kucoin),
}


async def check_key(client, market: str = "perpetual") -> KeyCheck:
    """Ask the exchange what the client's key may do. Never raises: a failure is KeyCheck.error."""
    native = getattr(client, "get_api_key_info", None)
    if callable(native):                                   # the native Bybit connector
        try:
            return read_bybit(await native(), market)
        except Exception as e:
            return KeyCheck("bybit", error=_short(e))
    x = getattr(client, "x", None)
    exchange = getattr(x, "id", None) or getattr(client, "venue_id", "?")
    if exchange == "hyperliquid" and x is not None:
        return await read_hyperliquid(x)
    reader = READERS.get(exchange)
    if reader is None or x is None:
        return KeyCheck(exchange, error="the relay cannot read this exchange's key permissions: check them on "
                                        "the exchange (trade only, no withdrawal, locked to the server's address)")
    call, read = reader
    try:
        answer = await getattr(x, call)()
    except Exception as e:
        return KeyCheck(exchange, error=_short(e))
    k = read(answer if isinstance(answer, dict) else {}, market)
    k.exchange = exchange
    return k


def _short(e: Exception) -> str:
    from relay.api.app import _scrub
    return _scrub(f"{type(e).__name__}: {e}")[:300]

