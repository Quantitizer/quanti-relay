"""What the relay knows about each exchange it can reach through CCXT (relay.venue.ccxt_venue).

A profile says how an exchange differs from the generic path: how its client order ids must look,
how its stop orders are asked for and found again, which test environment it offers, which keys it
needs, and what it can do (capabilities). An exchange with no profile here runs on the generic
profile, which assumes nothing beyond CCXT's unified interface.

`verified` is the honest part: a profile is verified only once the connector has been run against
the exchange's demo or test account (docs/venues.md says how). Until then it is a community
connector, and `relay check-config` says so.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capabilities:
    """What a venue can do. The relay refuses a setting the venue cannot carry out (a trailing stop
    on a venue without one) instead of pretending."""
    native_stop: bool = True              # a stop order that rests on the exchange
    trailing_stop: bool = False
    reduce_only_limit: bool = True        # take-profit ladder levels (resting reduce-only limits)
    closed_pnl: bool = False              # the venue's own realised-PnL records (else PnL from fills)
    funding_history: bool = False
    one_way: bool = True                  # one position per symbol (set, or the venue's only mode)
    set_leverage: bool = True             # leverage set through the API (CCXT's unified call works)
    set_margin_mode: bool = True          # isolated / cross set through the API


@dataclass(frozen=True)
class Profile:
    exchange: str                         # the CCXT id
    title: str
    verified: bool = False
    capabilities: Capabilities = field(default_factory=Capabilities)
    # How the relay's client order id (rl-d<deployment>-<hex>) must be written for this venue:
    #   plain  - as it is (letters, digits and dashes, at most 36)
    #   alnum  - letters and digits only, at most 32 (OKX)
    #   prefix_t - "t-" in front (Gate requires it)
    #   hex128 - a 128-bit hex id (Hyperliquid)
    #   int63  - an 18-digit number (HTX reads its client order id as an integer)
    client_id: str = "plain"
    # Extra parameters for a stop order (e.g. trigger on the mark price), and for listing the
    # resting stop orders (CCXT's unified flag is {"trigger": True}).
    stop_params: dict = field(default_factory=dict)
    trigger_list_params: dict = field(default_factory=lambda: {"trigger": True})
    # Where the venue keeps its kinds of trigger order apart, one listing per kind (None: the
    # one listing above). Cancelling and fetching one by id tries these too.
    trigger_listings: tuple[dict, ...] | None = None
    # Parameter sets that reach one trigger order by id (cancel, fetch), tried first where the
    # venue needs more than the listing's (Bitget: the trigger flag and the plan type together).
    trigger_cancel_params: tuple[dict, ...] | None = None
    # How a stop / take-profit price is asked for:
    #   unified   - CCXT's stopLossPrice / takeProfitPrice (CCXT picks the trigger direction)
    #   direction - a plain triggerPrice, plus the parameters saying which way the price must
    #               cross it: trigger_direction["falling"] for a long's stop (and a short's
    #               take-profit), ["rising"] for the opposite (MEXC)
    # Extra parameters for every resting trigger order (stop and take-profit), e.g. how long it
    # lives; and, where the venue lets a trigger order expire, its lifetime: the relay renews one
    # that is past 80% of it (reported as missing, so the stop audit re-places it, new before old).
    trigger_params: dict = field(default_factory=dict)
    trigger_max_age_s: int | None = None
    # How all resting trigger orders of a symbol are cancelled at once (None: with
    # trigger_list_params). MEXC picks its plan-order endpoint by a method name.
    cancel_all_trigger_params: dict | None = None
    stop_style: str = "unified"
    trigger_direction: dict = field(default_factory=dict)
    # Where CCXT leaves an order's fields unread: {unified field: field of the order's raw 'info'}
    # (MEXC's client id is in info.externalOid), and the venue's raw side codes as (side, reduces)
    # (MEXC: 1 open long, 2 close short, 3 open short, 4 close long).
    order_info: dict = field(default_factory=dict)
    order_sides: dict = field(default_factory=dict)
    # A market order must carry a reference price, from which the venue caps the slippage
    # (Hyperliquid): the last price for an order now, the trigger price for a stop.
    market_price: bool = False
    # Spot (venue.market: spot): the coin it is bought with, and how its resting stop orders are
    # listed (Binance lists spot stops among the plain open orders).
    spot_quote: str = "USDT"
    spot_trigger_list_params: dict = field(default_factory=lambda: {"trigger": True})
    spot_trigger_listings: tuple[dict, ...] | None = None
    # The parameter that makes a spot stop-market sell (Bitget's classic spot takes triggerPrice,
    # not stopLossPrice), extra parameters for it, and its lifetime where it expires (Gate: a day).
    spot_stop_param: str = "stopLossPrice"
    spot_trigger_params: dict = field(default_factory=dict)
    spot_trigger_max_age_s: int | None = None
    # False: the relay does not trade this venue's spot market (CCXT cannot place a spot stop
    # there safely).
    spot_supported: bool = True
    # The field of a fill (CCXT trade 'info') holding the realised PnL of that fill: with it, the
    # venue's closes are priced from its own records (closed_pnl); without it, a close the venue
    # made (a stop that fired) stays unpriced, and a daily loss limit cannot be checked there.
    realized_pnl_field: str | None = None
    # Whether the venue's balance "total" already includes unrealised PnL (else it is added).
    balance_includes_upl: bool = False
    # Test environments: "demo" (CCXT enable_demo_trading) and/or "testnet" (CCXT sandbox mode).
    environments: tuple[str, ...] = ()
    # Credentials beyond key and secret: "password" (a passphrase: OKX, Bitget, KuCoin), or a
    # wallet ("walletAddress" + "privateKey": Hyperliquid).
    credentials: tuple[str, ...] = ("apiKey", "secret")
    # The settlement coin of the perpetuals the relay trades there (and of the balance it reads).
    settle: str = "USDT"
    notes: str = ""


GENERIC = Profile(exchange="", title="generic CCXT exchange")

# The top 10 by perpetual-futures volume and open interest (checked 2026-11-03). Bybit has its own
# native connector (relay.exchange.bybit); its profile here is for the CCXT path, as a reference.
PROFILES: dict[str, Profile] = {p.exchange: p for p in (
    Profile("binance", "Binance USDⓈ-M futures", environments=("demo", "testnet"),
            stop_params={"workingType": "MARK_PRICE"}, realized_pnl_field="realizedPnl", spot_trigger_list_params={},
            capabilities=Capabilities(trailing_stop=True, closed_pnl=True, funding_history=True),
            notes="Conditional orders are listed with the trigger flag."),
    Profile("bybit", "Bybit (through CCXT; the relay's native Bybit connector is preferred)",
            spot_trigger_list_params={"orderFilter": "tpslOrder"},
            environments=("demo", "testnet"), stop_params={"triggerBy": "MarkPrice"},
            capabilities=Capabilities(trailing_stop=False, closed_pnl=False, funding_history=True)),
    Profile("okx", "OKX perpetual swaps", environments=("testnet",), client_id="alnum",
            trigger_listings=({"ordType": "conditional"}, {"trailing": True}),
            spot_trigger_listings=({"ordType": "conditional"},),
            credentials=("apiKey", "secret", "password"), balance_includes_upl=True,
            stop_params={"slTriggerPxType": "mark"}, realized_pnl_field="fillPnl",
            capabilities=Capabilities(trailing_stop=True, closed_pnl=True, funding_history=True),
            notes="OKX's demo trading is CCXT's sandbox mode; it needs demo-trading keys. Stops are "
                  "conditional algo orders (listed with ordType conditional; trailing apart). On spot, "
                  "CCXT sends a stop in cross mode (OKX refuses cash for it): verify on a test account."),
    Profile("bitget", "Bitget USDT-M futures", environments=("demo",),
            trigger_listings=({"planType": "profit_loss"}, {"trailing": True}), spot_stop_param="triggerPrice",
            trigger_cancel_params=({"trigger": True, "planType": "pos_loss"}, {"trigger": True, "planType": "pos_profit"},
                                   {"trailing": True}),
            credentials=("apiKey", "secret", "password"), realized_pnl_field="profit",
            # funding_history off: CCXT's Bitget funding history needs a symbol per call, and the
            # relay reads the whole account's (funding stays unknown, never 0).
            capabilities=Capabilities(trailing_stop=True, closed_pnl=True, funding_history=False)),
    Profile("gate", "Gate USDT perpetuals", environments=("testnet",), client_id="prefix_t",
            spot_trigger_max_age_s=86400,
            capabilities=Capabilities(funding_history=True)),
    Profile("mexc", "MEXC futures", environments=(), stop_style="direction",
            trigger_direction={"rising": {"triggerType": 1}, "falling": {"triggerType": 2}},
            stop_params={"trend": 2}, trigger_params={"executeCycle": 2}, trigger_max_age_s=7 * 24 * 3600,
            cancel_all_trigger_params={"method": "contractPrivatePostPlanorderCancelAll"},
            order_info={"clientOrderId": "externalOid"},
            order_sides={"1": ("buy", False), "2": ("buy", True), "3": ("sell", False), "4": ("sell", True)},
            capabilities=Capabilities(funding_history=True, set_leverage=False, set_margin_mode=False),
            notes="The futures trading API reopened on 2026-03-31 (MEXC's API change log). Stops are "
                  "plan orders triggered on the fair price; they live 7 days (executeCycle 2) and the "
                  "relay renews them before that. Leverage and margin mode are set on the exchange "
                  "(CCXT's calls need MEXC-specific arguments). Whether MEXC returns the relay's tag "
                  "on plan orders is checked at the first stop: if it does not, stops are refused. "
                  "No test environment: it stays a community connector until verified with a small "
                  "real account."),
    Profile("kucoinfutures", "KuCoin futures", environments=(),
            credentials=("apiKey", "secret", "password"),
            # funding_history off: as on Bitget, CCXT needs a symbol per call (kucoin.py).
            capabilities=Capabilities(funding_history=False)),
    Profile("htx", "HTX USDT-margined swaps", environments=(), client_id="int63",
            trigger_list_params={"stopLoss": True},
            trigger_listings=({"stopLoss": True}, {"takeProfit": True}, {"trailing": True}), spot_supported=False,
            notes="Stops, take-profits and trailing stops are separate algo-order lists. Spot is not "
                  "offered: CCXT would send HTX a spot stop as an immediate market sell.",
            capabilities=Capabilities(trailing_stop=True, funding_history=True)),
    Profile("bingx", "BingX perpetual futures", environments=("testnet",), trigger_list_params={},
            spot_trigger_list_params={},
            capabilities=Capabilities(trailing_stop=True, funding_history=True),
            notes="BingX's test environment is its VST demo account (CCXT sandbox mode)."),
    Profile("hyperliquid", "Hyperliquid perpetuals", environments=("testnet",), client_id="hex128",
            credentials=("walletAddress", "privateKey"), settle="USDC", realized_pnl_field="closedPnl",
            market_price=True, spot_quote="USDC", trigger_list_params={},
            capabilities=Capabilities(closed_pnl=True, funding_history=True),
            notes="Settles in USDC: a signal's BTCUSDT trades the BTC/USDC perpetual."),
)}


# The credentials the relay can read from the environment (relay.venue.factory names them).
KNOWN_CREDENTIALS = ("apiKey", "secret", "password", "uid", "login", "token", "walletAddress", "privateKey")


def profile_for(exchange: str) -> Profile:
    """The exchange's profile, or the generic one (with its CCXT id) when there is none. The generic
    one asks for the credentials CCXT says the exchange needs (a passphrase on some), not only a
    key and a secret."""
    if exchange in PROFILES:
        return PROFILES[exchange]
    credentials: tuple[str, ...] = ("apiKey", "secret")
    with contextlib.suppress(Exception):     # CCXT missing, or not one of its exchanges
        import ccxt
        required = getattr(ccxt, exchange)().requiredCredentials
        credentials = tuple(c for c in KNOWN_CREDENTIALS if required.get(c)) or credentials
    return Profile(exchange=exchange, title=f"{exchange} (generic CCXT)", credentials=credentials)
