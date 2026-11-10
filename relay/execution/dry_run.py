"""Dry-run flag resolution shared by the execution components.

A component's `dry_run` is a bool, or a (sync or async) callable returning the CURRENT flag, so a
runtime switch (deployment config reload, kill switch) takes effect without rebuilding it.
"""
import inspect
from collections.abc import Awaitable, Callable

DryRunFlag = bool | Callable[[], bool | Awaitable[bool]]


async def resolve_dry_run(flag: DryRunFlag) -> bool:
    if callable(flag):
        v = flag()
        return bool(await v) if inspect.isawaitable(v) else bool(v)
    return bool(flag)
