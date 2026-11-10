"""Order-link ownership tags.

Every position-opening order the relay places carries a client order id that encodes the owning
deployment: `rl-d{deployment_id}-{random}`. The position importer reads it back from the order
history, so a position the relay opened is attributed to its deployment deterministically, even if
the process died before it wrote the row. A position with no such tag was opened outside the relay.
The `rl-` prefix also lets a startup sweep find the relay's own resting orders and leave everyone
else's alone.
"""
from __future__ import annotations

import re
import uuid

PREFIX = "rl-"
_LINK = re.compile(r"rl-d(\d+)-")


def make_order_link(deployment_id: int | None) -> str:
    """Client order id that encodes the owner. At most 36 characters (Bybit's limit)."""
    return f"{PREFIX}d{deployment_id if deployment_id is not None else 0}-{uuid.uuid4().hex[:14]}"


def parse_order_link(link: str | None) -> int | None:
    """The deployment id encoded in a relay order link, or None if the order isn't the relay's."""
    m = _LINK.match(str(link or ""))
    return int(m.group(1)) if m else None
