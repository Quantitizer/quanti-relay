"""Template: a source plugin (docs/plugins/sources.md).

A source plugin lets a sender post its own JSON instead of the relay's v1 signal. It only
translates: the relay has already checked who sent the request (the source's hmac signature or
passphrase) before the plugin sees it, and it fills in the source name itself afterwards.

This template reads an imagined "simple alert" format:

    {"id": "a-123", "deployment": "main", "time": "2026-11-03T12:00:00Z",
     "ticker": "BTCUSDT", "action": "buy" | "sell" | "exit", "usd": 500, "stop_pct": 2}

and turns it into a v1 event signal. Copy this file into your own package, rename the class,
change `translate`, register it (see below) and run the contract checks
(relay.plugins.contract.check_source) in your package's tests.

Registering it, in your package's pyproject.toml:

    [project.entry-points."quanti_relay.sources"]
    simple-alert = "your_package.module:SimpleAlertSource"

and in the relay's source file: `format: simple-alert`. The sender then posts to
POST /v1/sources/<source name>/signals (its JSON carries no "source" field).

Rules for a translation:
- No I/O and no state: the same payload always gives the same signal.
- Never invent a trading number. A size or a stop the payload does not carry stays out of the
  signal (the deployment's own rules then apply, or the relay refuses the signal).
- Raise ValueError with a plain message for a payload you cannot read; the relay answers 422.
"""
from __future__ import annotations

from typing import Any

_ACTIONS = {"buy": ("open", "long"), "sell": ("open", "short"), "exit": ("close", None)}


class SimpleAlertSource:
    name = "simple-alert"

    def translate(self, payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("id", "deployment", "time", "ticker", "action"):
            if not payload.get(key):
                raise ValueError(f"the alert has no {key}")
        action = _ACTIONS.get(str(payload["action"]).lower())
        if action is None:
            raise ValueError(f"unknown action {payload['action']!r} (buy, sell or exit)")
        verb, side = action
        signal: dict[str, Any] = {
            "version": "1", "id": str(payload["id"]), "deployment": str(payload["deployment"]),
            "sent_at": str(payload["time"]), "kind": "event", "action": verb, "symbol": str(payload["ticker"]),
        }
        if side is not None:
            signal["side"] = side
            if payload.get("usd") is not None:
                signal["size"] = {"notional_usd": payload["usd"]}
            if payload.get("stop_pct") is not None:
                signal["stop"] = {"pct": payload["stop_pct"]}
        return signal
