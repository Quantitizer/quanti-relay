"""Setting the relay up from the console: what is configured and what is missing, the exchanges on
offer, writing the config files, checking a venue on a test account, and the switch to live.

The config files stay the one source of truth (docs/SPEC.md §7). The console writes the same YAML a
person would, after the same checks the relay makes at start, and keeps the version it replaced in
`<config_dir>/.history/`. Three things it never does:

- **hold a secret.** Keys, tokens and passphrases stay in environment variables. The console names
  the variable and says whether it is set, never what it holds.
- **switch to live on its own.** Going live writes `dry_run: false` only after the gates below pass
  and the owner types the deployment's name, and the environment (RELAY_DRY_RUN) must say live as
  well: that second key is set by the owner outside the console, followed by a restart.
- **preset a trading number.** Every number the owner does not fill in stays empty, and empty is off.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from relay.deployment_config import DeploymentConfigError, DeploymentFile, read_yaml

KINDS = {"deployment": "deployments", "source": "sources", "sink": "sinks"}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
HISTORY_KEEP = 20
HEADER = ("# Written by the relay console. You may edit it by hand too; the console keeps the version\n"
          "# it replaced in .history/ next to this folder. Secrets never go here: name the variable.\n")

# The limits that must be filled in before a deployment may go live: how big one position may be,
# how big the whole book may be, how many positions, and how much may be lost in a day.
LIVE_LIMITS = (
    ("rails.max_notional_usd|rails.max_notional_pct", "the largest position (USDT or % of equity)"),
    ("rails.max_gross_leverage", "the largest book (gross leverage)"),
    ("risk.max_positions", "how many positions at once"),
    ("risk.daily_loss_limit_pct", "the daily loss limit"),
)


class SetupError(ValueError):
    """A change the console will not make (the message says why)."""


# A source or sink written here names only variables of its own: RELAY_SOURCE_<NAME>_... or
# RELAY_SINK_<NAME>_... . A sink pointed at someone else's variable would send it out (a Telegram
# token goes into the request's URL), and a source pointed at one would make it a signing secret:
# an exchange key, the API key or another source's secret must never be reachable that way.
def own_prefix(kind: str, name: str) -> str:
    return f"RELAY_{kind.upper()}_{name.upper().replace('-', '_')}_"


def _variables_named(data: dict) -> list[str]:
    return [v for k, v in data.items() if k.endswith("_env") and isinstance(v, str)]


# ---- what is set up ---------------------------------------------------------------------------------

def _get(data: dict, dotted: str) -> Any:
    for part in dotted.split("."):
        data = (data or {}).get(part) if isinstance(data, dict) else None
    return data


def missing_limits(deployment: DeploymentFile | None) -> list[str]:
    """The limits LIVE_LIMITS asks for that are still empty."""
    if deployment is None:
        return [what for _, what in LIVE_LIMITS]
    data = deployment.model_dump()
    return [what for keys, what in LIVE_LIMITS if all(_get(data, k) in (None, "") for k in keys.split("|"))]


def writable(config_dir: str | Path) -> bool:
    """Whether the relay may write its config folder (in Docker it is mounted read-only by default)."""
    folder = Path(config_dir)
    return folder.is_dir() and os.access(folder, os.W_OK)


def key_status(deployment: DeploymentFile | None, environ=None) -> list[dict[str, Any]]:
    """[{variable, set}] for the keys this deployment's venue reads (never their values)."""
    environ = os.environ if environ is None else environ
    if deployment is None or deployment.venue.native:
        names = ["BYBIT_API_KEY", "BYBIT_API_SECRET"]
    elif deployment.venue.exchange in _ccxt_ids():
        from relay.venue.factory import key_env_names
        from relay.venue.profiles import profile_for
        names = list(key_env_names(profile_for(deployment.venue.exchange)).values())
    else:
        return []                                      # a venue plugin reads its own variables
    return [{"variable": n, "set": bool(environ.get(n))} for n in names]


def _ccxt_ids() -> set[str]:
    try:
        import ccxt
        return set(ccxt.exchanges)
    except Exception:
        return set()


def catalog() -> dict[str, Any]:
    """The exchanges the console offers: the profiled ones with what they can do, and every other
    CCXT id (a community connector with the generic profile)."""
    from relay.venue.factory import key_env_names
    from relay.venue.profiles import PROFILES
    profiled = []
    for p in PROFILES.values():
        c = p.capabilities
        profiled.append({
            "exchange": p.exchange, "title": p.title, "verified": p.verified,
            "environments": [*p.environments, "live"], "keys": list(key_env_names(p).values()),
            "spot": p.spot_supported, "notes": p.notes,
            "can": {"stop": c.native_stop, "trailing": c.trailing_stop, "ladder": c.reduce_only_limit,
                    "leverage": c.set_leverage, "margin_mode": c.set_margin_mode, "pnl": c.closed_pnl,
                    "funding": c.funding_history},
        })
    others = sorted(_ccxt_ids() - set(PROFILES))
    return {"native": {"exchange": "bybit", "title": "Bybit (native connector)", "verified": True,
                       "environments": ["demo", "testnet", "live"], "keys": ["BYBIT_API_KEY", "BYBIT_API_SECRET"]},
            "profiled": profiled, "others": others}


def on_disk(config_dir: str | Path, environ=None) -> dict[str, Any]:
    """The config as the files say now (what the next start will run), which may differ from what
    the running relay read at its start: the deployment (or the reason it cannot be used), and the
    sources and sinks as written, each with whether its variables are set (never their values)."""
    from relay.deployment_config import load_deployment
    environ = os.environ if environ is None else environ
    out: dict[str, Any] = {"deployment": None, "deployment_problem": None, "sources": [], "sinks": []}
    try:
        out["deployment"] = load_deployment(config_dir)
    except DeploymentConfigError as e:
        out["deployment_problem"] = str(e)
    for kind in ("sources", "sinks"):
        folder = Path(config_dir) / kind
        for path in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]) if folder.is_dir() else []:
            try:
                raw = read_yaml(path.read_text()) or {}
            except yaml.YAMLError:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            variables = {v: bool(environ.get(v)) for v in _variables_named(raw)}
            out[kind].append({"name": raw.get("name", path.stem), "settings": raw, "variables": variables})
    return out


def live_on_disk(config_dir: str | Path) -> bool:
    """Whether a deployment file says live (dry_run: false). One that cannot be read counts as live:
    what it says is not known."""
    folder = Path(config_dir) / "deployments"
    for path in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]) if folder.is_dir() else []:
        try:
            data = read_yaml(path.read_text())
        except (OSError, yaml.YAMLError):
            return True
        if not isinstance(data, dict) or data.get("dry_run", True) is not True:
            return True
    return False


# ---- writing the files ------------------------------------------------------------------------------

def _validate(kind: str, name: str, data: dict, environ) -> None:
    """The checks the relay makes at start, on the new file alone (in a scratch folder). A secret
    not set yet is not a reason to refuse the file: the owner sets it next, then restarts."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / KINDS[kind]
        folder.mkdir()
        (folder / f"{name}.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        if kind == "deployment":
            from relay.deployment_config import load_deployment
            load_deployment(tmp)
        elif kind == "source":
            from relay.signals.sources import load_sources
            stand_in = {**environ, data.get("secret_env") or "": "x" * 64}
            load_sources(tmp, environ=stand_in)
        else:
            from relay.sinks import load_sinks
            stand_in = dict(environ)
            for k, v in data.items():
                if k.endswith("_env") and isinstance(v, str) and not stand_in.get(v):
                    stand_in[v] = "https://stand-in.invalid/" if "url" in k else "stand-in"
            load_sinks(tmp, environ=stand_in)


def _deployment_path(config_dir: Path, name: str) -> Path:
    folder = config_dir / "deployments"
    existing = sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]) if folder.is_dir() else []
    if existing:
        current = read_yaml(existing[0].read_text()) or {}
        if current.get("name", existing[0].stem) != name:
            raise SetupError(f"the deployment is {current.get('name', existing[0].stem)!r}: its name is not changed "
                             f"here (positions belong to it)")
        return existing[0]
    return folder / f"{name}.yaml"


def _keep_dry_run(path: Path, data: dict) -> bool:
    """The dry_run a saved deployment keeps: the file's own. A live file stays live only while what
    the gates checked still holds: the same exchange, market and environment, and every core limit
    filled in. Otherwise it goes back to paper, and going live is asked again (set_dry_run)."""
    current = (read_yaml(path.read_text()) or {}) if path.exists() else {}
    if current.get("dry_run", True) is not False:
        return True
    try:
        new = DeploymentFile.model_validate(data | {"dry_run": True})
        old = DeploymentFile.model_validate(current)
    except Exception:
        return True                                  # it is refused below anyway
    return bool(new.venue != old.venue or missing_limits(new))       # True = back to paper


def write_config(config_dir: str | Path, kind: str, name: str, data: dict, *, environ=None) -> Path:
    """Check and write one config file; the version it replaces goes to .history/. A deployment keeps
    the dry_run its file has, unless a live one would change what going live checked
    (_keep_dry_run); going live has its own way, set_dry_run. Raises SetupError."""
    environ = os.environ if environ is None else environ
    if kind not in KINDS:
        raise SetupError(f"unknown kind {kind!r}")
    if not isinstance(name, str) or not NAME.match(name):
        raise SetupError("a name is 1-50 lowercase letters, digits, '-' or '_'")
    if not isinstance(data, dict):
        raise SetupError("the settings must be an object")
    config_dir = Path(config_dir)
    if not writable(config_dir):
        raise SetupError(f"the config folder {config_dir} cannot be written by the relay (docs: README, "
                         f"'Setting up in the console')")
    data = {k: v for k, v in data.items() if v is not None}
    if kind == "deployment":
        data["name"] = name
        path = _deployment_path(config_dir, name)
        data["dry_run"] = _keep_dry_run(path, data)
    else:
        prefix = own_prefix(kind, name)
        foreign = [v for v in _variables_named(data) if not v.startswith(prefix) or v == prefix]
        if foreign:
            raise SetupError(f"{', '.join(foreign)}: a {kind} written here names variables of its own only, starting "
                             f"with {prefix} (never an exchange key, the API key or another {kind}'s)")
        disk = on_disk(config_dir, environ)
        for other in disk["sources"] + disk["sinks"]:
            if other["name"] != name and set(other["variables"]) & set(_variables_named(data)):
                raise SetupError(f"the {other['name']} file already uses {', '.join(set(other['variables']))}")
        if kind == "source":
            data["name"] = name                     # the file's name: two files can never claim one name
            if any(x["name"] == name for x in disk["sources"]) and not (config_dir / "sources" / f"{name}.yaml").exists():
                raise SetupError(f"another source file is already named {name!r}")
        path = config_dir / KINDS[kind] / f"{name}.yaml"
    try:
        _validate(kind, name, data, environ)
    except (DeploymentConfigError, ValueError) as e:
        raise SetupError(str(e).replace(str(Path(tempfile.gettempdir())), "<check>")) from None
    _write(config_dir, path, HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


def remove_config(config_dir: str | Path, kind: str, name: str) -> None:
    """Remove a source or sink file (its last version stays in .history/)."""
    if kind not in ("source", "sink"):
        raise SetupError("only a source or a sink is removed here")
    if not NAME.match(name or ""):
        raise SetupError("unknown name")
    config_dir = Path(config_dir)
    path = config_dir / KINDS[kind] / f"{name}.yaml"
    if not path.is_file():
        raise SetupError(f"there is no {kind} {name}")
    if not writable(config_dir):
        raise SetupError(f"the config folder {config_dir} cannot be written by the relay")
    _keep(config_dir, path)
    path.unlink()


def set_dry_run(config_dir: str | Path, dry_run: bool) -> Path:
    """Write dry_run into the deployment file (nothing else changes)."""
    config_dir = Path(config_dir)
    folder = config_dir / "deployments"
    files = sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]) if folder.is_dir() else []
    if len(files) != 1:
        raise SetupError("there is no deployment file to change: set the deployment up first")
    if not writable(config_dir):
        raise SetupError(f"the config folder {config_dir} cannot be written by the relay")
    path = files[0]
    data = read_yaml(path.read_text()) or {}
    data["dry_run"] = bool(dry_run)
    _write(config_dir, path, HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


def _write(config_dir: Path, path: Path, text: str) -> None:
    """Replace the file in one step (a crash leaves the old one or the new one, never half), keeping
    the old one in .history/. The same text again changes nothing (a step repeated is harmless)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return
    if path.exists():
        _keep(config_dir, path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644     # readable by its owner's tools too
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".console-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _keep(config_dir: Path, path: Path) -> None:
    history = config_dir / ".history"
    history.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copy2(path, history / f"{path.parent.name}-{path.stem}-{stamp}.yaml")
    mine = re.compile(rf"^{re.escape(path.parent.name)}-{re.escape(path.stem)}-\d{{8}}T\d{{12}}Z\.yaml$")
    kept = sorted(p for p in history.iterdir() if mine.match(p.name))       # not another name's history
    for old in kept[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)


# ---- going live -------------------------------------------------------------------------------------

def live_gates(*, deployment: DeploymentFile | None, keys_set: bool, venue_checked: bool | None,
               halted: str | None) -> list[dict[str, Any]]:
    """What must hold before the console writes dry_run: false. venue_checked None: not asked of this
    venue (the native Bybit connector, verified by the project; a plugin, checked by its author)."""
    missing = missing_limits(deployment)
    gates = [
        {"gate": "deployment", "ok": deployment is not None,
         "how": "set the deployment up (Setup: exchange and limits)"},
        {"gate": "keys", "ok": keys_set, "how": "set the exchange's keys in the environment (.env) and restart"},
        {"gate": "limits", "ok": not missing,
         "how": "fill in: " + ", ".join(missing) if missing else "filled in"},
        {"gate": "venue_check", "ok": venue_checked is not False,
         "how": "run the venue check on this exchange's test account (Setup: test)"},
        {"gate": "not_halted", "ok": not halted, "how": "resume after the emergency stop first"},
    ]
    if deployment is not None and deployment.venue.environment in ("demo", "testnet"):
        gates.append({"gate": "environment", "ok": True,
                      "how": f"note: venue.environment is {deployment.venue.environment}: live orders go to "
                             f"that test account, not real money"})
    return gates
