"""NOT WIRED YET: the running relay does not use this module. It is kept for the venue-interface phase (symbol mapping across venues), and reviewed and fixed when it is connected (tests/test_unwired_modules.py keeps the running relay from importing it before then).


Symbol and pair_id utilities.
"""


def construct_pair_id(venue: str, symbol: str) -> str:
    """
    Construct a pair_id from venue and symbol.
    
    Args:
        venue: Venue identifier (e.g., "binance", "coinbase")
        symbol: Trading symbol (e.g., "BTC/USDT", "ETH/USD")
    
    Returns:
        Pair ID in format "venue:symbol" (e.g., "binance:BTC/USDT")
    """
    return f"{venue}:{symbol}"


def parse_pair_id(pair_id: str) -> tuple[str, str]:
    """
    Parse a pair_id into venue and symbol components.
    
    Args:
        pair_id: Pair ID in format "venue:symbol"
    
    Returns:
        Tuple of (venue, symbol)
    
    Raises:
        ValueError: If pair_id format is invalid
    
    Example:
        parse_pair_id("binance:BTC/USDT") -> ("binance", "BTC/USDT")
    """
    parts = pair_id.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid pair_id format: {pair_id}")
    return parts[0], parts[1]


def normalize_symbol(symbol: str) -> str:
    """
    Normalize a trading symbol to standard format.
    
    Handles variations like:
        - "BTC-USDT" -> "BTC/USDT"
        - "btcusdt" -> "BTC/USDT" (requires heuristics)
        - "BTC_USDT" -> "BTC/USDT"
    
    Args:
        symbol: Trading symbol in various formats
    
    Returns:
        Normalized symbol in "BASE/QUOTE" format
    """
    # Replace common separators with /
    symbol = symbol.replace("-", "/").replace("_", "/")
    
    # Uppercase
    symbol = symbol.upper()
    
    # If no separator, try to split common quote currencies
    if "/" not in symbol:
        # Common quote currencies to check
        quotes = ["USDT", "USDC", "USD", "BTC", "ETH", "BNB", "EUR"]
        for quote in quotes:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base = symbol[:-len(quote)]
                return f"{base}/{quote}"
    
    return symbol


def extract_base_quote(symbol: str) -> tuple[str, str]:
    """
    Extract base and quote currencies from a symbol.
    
    Args:
        symbol: Trading symbol (e.g., "BTC/USDT")
    
    Returns:
        Tuple of (base, quote)
    
    Raises:
        ValueError: If symbol format is invalid
    """
    normalized = normalize_symbol(symbol)
    parts = normalized.split("/")
    if len(parts) != 2:
        raise ValueError(f"Cannot extract base/quote from symbol: {symbol}")
    return parts[0], parts[1]


def is_stablecoin(asset_symbol: str) -> bool:
    """
    Check if an asset is a stablecoin.
    
    Args:
        asset_symbol: Asset symbol (e.g., "USDT", "USDC", "DAI")
    
    Returns:
        True if it's a known stablecoin
    """
    stablecoins = {
        "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "USDD",
        "FRAX", "LUSD", "SUSD", "GUSD", "PYUSD"
    }
    return asset_symbol.upper() in stablecoins

