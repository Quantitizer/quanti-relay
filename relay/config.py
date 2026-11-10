"""Process-wide settings, read from environment variables (prefix `RELAY_`) or a `.env` file.

Defaults are deliberately safe: dry-run and testnet are on. Exchange credentials are never
defined here — venue adapters read them from the environment themselves, so they can't end up
in a settings dump or a log line.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELAY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Safety defaults — a fresh install never trades real money.
    dry_run: bool = True
    testnet: bool = True

    # Storage. SQLite (one file, nothing to install) unless a Postgres URL is given, e.g.
    # postgresql+asyncpg://user:pass@host/db (needs the `postgres` extra).
    database_url: str = "sqlite+aiosqlite:///./relay.db"

    # API access. Every endpoint except /health needs a key (header `Authorization: Bearer <key>`
    # or `X-API-Key`). `api_key` may do everything; `api_readonly_key` may only read. With no key
    # set, the API refuses all requests unless env == "development" (local convenience only).
    env: str = "production"
    api_key: str = ""
    api_readonly_key: str = ""

    # Reaching the relay from the internet (relay.api.guard). Not trading values: they protect the API.
    # Behind a TLS proxy you run: its address(es) or network(s), comma-separated, and the header it
    # puts the client's address in. Without them the client is the direct peer.
    trusted_proxies: str = ""
    client_ip_header: str = "X-Forwarded-For"
    # Optional: only these addresses or networks (comma-separated) may use the API, the console and
    # the API documentation. Signal intake follows each source's own rules; /health stays open.
    api_allow_ips: str = ""
    # Wrong API keys: this many from one address within the window lock it out for auth_lockout_s
    # (0 = never locked out).
    auth_max_failures: int = 10
    auth_failure_window_s: int = 600
    auth_lockout_s: int = 900
    max_body_bytes: int = 1_048_576     # largest request body accepted (0 = no limit)
    max_connections: int = 100          # connections served at once; more are answered 503
    # A dead-man switch: while the relay is healthy (as /health says) it calls this URL every
    # heartbeat_interval_s, so a monitoring service you choose can alert you when the calls stop.
    heartbeat_url: str = ""
    heartbeat_interval_s: int = 60
    # Written by deploy/install.sh: the address the relay is reached at (its TLS name or IP) and the
    # server's public IP (relay doctor checks the exchange key is locked to it).
    public_host: str = ""
    public_ip: str = ""
    # The MCP endpoint for chat apps (relay.api.mcp, signed in with relay.api.oauth). Off unless set.
    mcp_enabled: bool = False

    # Folder with the relay's config files: sources/<name>.yaml (who may send signals; see
    # relay.signals.sources). Relative paths are from the working directory.
    config_dir: str = "config"

    # The built-in default deployment (one account, one deployment) until deployment config files
    # arrive. Its dry-run state is `dry_run` above.
    deployment_name: str = "default"
    default_leverage: float | None = None  # unset: the account's own leverage is used, never changed
    # Margin mode per symbol: true = isolated, false = cross, unset = the account's mode is never
    # touched. (Bybit unified accounts set margin mode account-wide; leave this unset there.)
    isolated_margin: bool | None = None
    require_stop: bool = True

    # Trading and risk limits. The relay ships NO values: each is the user's own input, and an unset
    # (empty) one means that check or feature is off.
    max_positions: int | None = None          # open positions of this mode
    max_exposure_pct: float | None = None     # open notional as % of available balance
    daily_loss_limit_pct: float | None = None  # today's realised loss as % of balance
    min_balance: float | None = None          # refuse entries below this available balance (USDT)
    cooldown_hours: float | None = None       # wait after closing a symbol before re-entering it
    min_stop_distance_pct: float | None = None  # refuse stops closer than this to the entry
    max_stop_distance_pct: float | None = None  # refuse stops further than this from the entry
    take_profit_pct: float | None = None      # default take-profit when a signal sends none
    # Default take-profit LADDER when an order sends no take-profit, as JSON, e.g.
    # [{"pct": 2, "close_pct": 50}, {"pct": 4, "close_pct": 50}]. Not together with take_profit_pct.
    take_profit_ladder: list[dict] | None = None
    # Trailing stop for new positions when an order sends none: it trails this % behind the best
    # price, starting once the position is trailing_activate_pct in profit (unset: at once). It runs
    # beside the fixed stop, which stays in place.
    trailing_distance_pct: float | None = None
    trailing_activate_pct: float | None = None
    # Break-even for new positions when an order sends none: once the position is
    # break_even_trigger_pct in profit, OR break_even_trigger_r times its initial risk (entry to the
    # opening stop; set one of the two), its stop moves to the entry price, plus
    # break_even_buffer_pct on the profit side (unset: exactly the entry). It only ever tightens.
    break_even_trigger_pct: float | None = None
    break_even_trigger_r: float | None = None
    break_even_buffer_pct: float | None = None
    break_even_trigger_tp: int | None = None    # or: once take-profit level n has filled (1 = the first)
    max_size_shortfall_pct: float | None = None  # refuse when venue lot rules shrink an order by more than this %
    max_hold_hours: float | None = None          # the time stop: close a position open this long, at market

    # Background jobs
    private_ws_enabled: bool = True     # live order/position feed (needs exchange keys)
    position_sync_interval_s: int = 60
    stop_audit_interval_s: int = 300
    stop_rules_interval_s: int = 10     # how often the relay checks prices for break-even
    pnl_reconcile_interval_s: int = 120
    notify_interval_s: int = 5          # how often events are handed to the notification sinks
    # The reconciler writes the venue-verified PnL (bookkeeping only; false = report what it would write)
    pnl_reconcile_writes: bool = True

    # Exchange-call circuit breaker: open after N consecutive failures, retry after `timeout`.
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_timeout: int = 60  # seconds

    # Bybit transport. Keys are NOT settings: BybitRestClient reads BYBIT_API_KEY / BYBIT_API_SECRET
    # from the environment. `testnet` above selects the testnet host; demo takes priority over it.
    bybit_demo: bool = False
    bybit_region: str = ""  # "hk" -> Hong Kong mainnet host
    bybit_recv_window_ms: int = 5000
    bybit_max_concurrent_requests: int = 6
    api_timeout: float = 30.0  # seconds, total per request
    api_connect_timeout: float = 10.0  # seconds
    bybit_ws_ping_interval: int = 20  # protocol-level ping, seconds (0 disables; app-level ping still runs)
    bybit_ws_ping_timeout: int = 45
    bybit_ws_pong_timeout: int = 45  # app-level: no pong for this long -> force reconnect

    # Hyperliquid transport. The agent-wallet key is NOT a setting: env HYPERLIQUID_AGENT_PRIVATE_KEY.
    hyperliquid_account_address: str = ""  # defaults to the agent wallet's address
    hyperliquid_vault_address: str = ""  # "" => plain account
    hyperliquid_use_ws: bool = False  # WS best-bid/offer cache for the maker-chase touch
    hyperliquid_min_interval_s: float = 0.5
    hyperliquid_max_concurrency: int = 1


settings = Settings()
