"""
Human-readable translations for common venue/execution errors, shown in API responses.

The venue's own text is always kept: a translation is a hint, never a replacement, so a wrong or
outdated mapping can never hide what the venue actually said. Numeric codes match only as whole
numbers ("0.4290" is not error 429).
"""

import re
from collections.abc import Callable


def humanize_order_error(raw_error: str) -> str:
    """
    Convert raw Bybit/API error strings into human-readable messages.

    Case-insensitive; numeric codes match as whole numbers; most specific patterns first. Returns
    "<plain message> (venue: <raw error>)", or the raw error when nothing matches.

    Args:
        raw_error: Raw error string from Bybit API or exception

    Returns:
        Human-readable message, or original if no match
    """
    if not raw_error:
        return "Unknown error"

    err_lower = raw_error.lower()

    # Compound patterns: (check_fn, message) - checked first
    compound_checks: list[tuple[Callable[[str], bool], str]] = [
        (lambda e: "params error" in e and "qty" in e,
         "Order size rejected: quantity doesn't match the symbol's precision or minimum size rules"),
        (lambda e: "order notional" in e and "below" in e,
         "Order value below the symbol's minimum notional value"),
        (lambda e: "orderqty" in e and "trunc" in e,
         "Order quantity truncated to zero (too small for symbol precision)"),
        (lambda e: "order amount" in e and "too large" in e,
         "Order size exceeds the symbol's maximum allowed amount"),
        (lambda e: "order quantity" in e and "too large" in e,
         "Order size exceeds the symbol's maximum allowed amount"),
        (lambda e: "order quantity" in e and "exceeds" in e,
         "Order size exceeds the symbol's risk limit"),
    ]

    for check_fn, message in compound_checks:
        if check_fn(err_lower):
            return f"{message} (venue: {raw_error})"

    # Simple patterns: (substring, message) - order matters
    simple_mappings: list[tuple[str, str]] = [
        # Order quantity / size
        ("qty invalid", "Order size rejected: quantity doesn't match the symbol's precision or minimum size rules"),
        ("below minimum", "Order size below the symbol's minimum allowed amount"),
        ("min order", "Order size below the symbol's minimum allowed amount"),
        ("110094", "Order value below the symbol's minimum notional value"),
        ("110016", "Order size exceeds the symbol's risk limit"),
        ("110021", "Position limit exceeded (Open Interest limit)"),
        ("110022", "Quantity restricted; cannot increase position size"),
        ("110023", "Reduce-only mode: can only reduce position on this contract"),
        ("110040", "Order would trigger forced liquidation; please reduce size"),
        ("110039", "Liquidation risk: maintenance margin rate too high"),
        ("110066", "Trading is not allowed for this contract"),
        ("110074", "Contract is not live for trading"),
        ("110118", "Unable to get price due to low liquidity"),
        # Balance
        ("insufficient", "Insufficient balance to place this order"),
        ("not enough", "Insufficient balance to place this order"),
        ("balance insufficient", "Insufficient balance to place this order"),
        ("110004", "Wallet balance insufficient"),
        ("110007", "Available balance insufficient"),
        ("110012", "Insufficient available balance"),
        ("110014", "Insufficient balance to add margin"),
        ("110044", "Available margin insufficient"),
        ("110045", "Wallet balance insufficient"),
        ("110006", "Assets cannot cover position margin"),
        # Order / position state
        ("order not found", "Order not found (may already be filled or cancelled)"),
        ("110001", "Order does not exist"),
        ("order not exist", "Order does not exist"),
        ("110008", "Order already completed or cancelled"),
        ("110010", "Order has been cancelled"),
        ("position not found", "No open position for this symbol"),
        ("no position", "No open position for this symbol"),
        ("110034", "No net position"),
        ("110030", "Duplicate order ID"),
        ("110009", "Too many stop orders; maximum limit reached"),
        ("110020", "Too many active orders (max 500)"),
        ("110058", "Cannot set TP/SL: remaining position size too small"),
        ("not modified", "Stop-loss already set or no change needed"),
        ("34040", "Stop-loss already set or no change needed"),
        # Rate limits
        ("rate limit", "Too many requests; please wait a moment and retry"),
        ("too many visits", "Too many requests; please wait a moment and retry"),
        ("too many requests", "Too many requests; please wait a moment and retry"),
        ("10006", "Too many requests; exceeded API rate limit"),
        ("10018", "Exceeded IP rate limit"),
        ("429", "Server overloaded; please retry in a moment"),
        # Circuit / blocked
        ("circuit breaker", "Order blocked temporarily: the venue was failing; retry shortly"),
        # Authentication
        ("10003", "Invalid API key; check key and domain"),
        ("10004", "Invalid signature; check API key setup"),
        ("10005", "Permission denied; check API key permissions"),
        ("10007", "User authentication failed"),
        ("10009", "IP banned"),
        ("10010", "IP address not allowed for this API key"),
        # Server / network
        ("10000", "Server timeout; please retry"),
        ("10016", "Server error; service may be restarting"),
        ("timeout", "Request timed out; please retry"),
        ("connection", "Connection to exchange failed; please retry"),
    ]

    for pattern, message in simple_mappings:
        hit = (re.search(rf"(?<![\d.]){pattern}(?![\d.])", err_lower) if pattern.isdigit()
               else pattern in err_lower)
        if hit:
            return f"{message} (venue: {raw_error})"

    return raw_error
