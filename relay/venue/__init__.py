"""Venues: the exchanges the relay trades on (Bybit natively, any other through CCXT)."""
import re

_ID = re.compile(r"^[a-z0-9]{2,30}$")


def venue_of(client) -> str:
    """The venue id of a venue client ('bybit' for the native Bybit client and test doubles). Only
    ever a plain lowercase id, so it can be written into SQL as a literal."""
    venue = getattr(client, "venue_id", None)
    return venue if isinstance(venue, str) and _ID.match(venue) else "bybit"
