"""Modules kept for a later phase (marked NOT WIRED YET) must not be used by the running relay until
they have been reviewed and connected. This loads the relay's entry points in a fresh interpreter
and checks none of them is imported, directly or indirectly."""
import json
import subprocess
import sys

UNWIRED = [
    "relay.venue.adapter",
    "relay.venue.hyperliquid_adapter",
    "relay.execution.keyed_book_importer",
    "relay.execution.book_reconcile",
    "relay.execution.smart_executor",
    "relay.util.rate_limiter",
    "relay.util.symbols",
    "relay.safety.guardian_job",
    "relay.safety.account_dd_guardian",
]


def test_the_running_relay_imports_no_unwired_module():
    code = ("import sys, json; import relay.api.app, relay.cli, relay.runtime; "
            f"print(json.dumps([m for m in {UNWIRED!r} if m in sys.modules]))")
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_every_unwired_module_says_so():
    import importlib
    for name in UNWIRED:
        assert importlib.import_module(name).__doc__.startswith("NOT WIRED YET"), name
