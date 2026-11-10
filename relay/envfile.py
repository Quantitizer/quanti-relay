"""The relay's secrets file (`.env`): set a secret without it passing through anyone else's hands.

  relay secret set NAME        asks for the value at the terminal, hidden, and writes it
  relay secret generate NAME   writes a new random value (an API key, a source's secret)
  relay secret reveal NAME     shows a value on the terminal itself (to copy it somewhere)
  relay secret list            the names in the file, never their values
  relay secret setting NAME VALUE   a plain setting that is no secret (an address, a switch): the
                                    install script writes these; a name that sounds secret is refused

An AI agent that runs commands for its owner can run these, but cannot read or type the values:
`set` and `reveal` talk to the terminal device (/dev/tty), not to the command's input and output,
so the owner types and reads them there. Without a terminal they refuse. Nothing here prints a
value to standard output or to a log.

This file uses Python's standard library only: on a server without the relay installed it runs as
`python3 relay/envfile.py ...` (the install script's `relay` wrapper does that). The file is
written in one step, readable by its owner only (0600), and every other line is kept as it was.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# Names that hold secrets: never given on the command line (it shows in the process list and the
# shell's history), only typed at the terminal (set) or made here (generate).
SECRET_WORDS = ("KEY", "SECRET", "PASSWORD", "PASSPHRASE", "TOKEN", "URL")
GENERATED_BYTES = 32          # token_urlsafe(32): 43 characters, above the 32 the relay asks for


class EnvFileError(ValueError):
    pass


def names(path: Path) -> dict[str, bool]:
    """{name: has a value} for every variable the file sets."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = bool(_unquote(m.group(2)))
    return out


def get(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    value = None
    for line in path.read_text().splitlines():
        m = _LINE.match(line)
        if m and m.group(1) == name:
            value = _unquote(m.group(2))             # the last one wins, as in docker compose
    return value


def put(path: Path, name: str, value: str) -> bool:
    """Set NAME to value (replacing every line that sets it). Returns False when it was already so."""
    if not NAME.match(name):
        raise EnvFileError("a name is 2-64 capital letters, digits or '_', starting with a letter")
    if not value or any(c in value for c in "'\n\r\0"):
        raise EnvFileError("the value must not be empty and must not hold a quote (') or a line break")
    if get(path, name) == value:
        return False
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) == name:
            if not done:
                out.append(f"{name}='{value}'")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{name}='{value}'")
    _write(path, "\n".join(out) + "\n")
    return True


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw.split(" #", 1)[0].strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---- the terminal -------------------------------------------------------------------------------

def _tty():
    """The terminal itself (not the command's input or output), or EnvFileError."""
    try:
        return open("/dev/tty", "r+")
    except OSError:
        raise EnvFileError("this needs a person at a terminal: run it in your own terminal (or your cloud "
                           "provider's browser console), not through an agent") from None


def ask_hidden(prompt: str) -> str:
    import termios
    tty = _tty()
    try:
        fd = tty.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~termios.ECHO
        tty.write(prompt)
        tty.flush()
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, new)
            value = tty.readline().rstrip("\r\n")
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
            tty.write("\n")
        return value.strip()
    finally:
        tty.close()


def show_on_tty(text: str) -> None:
    tty = _tty()
    try:
        tty.write(text + "\n")
    finally:
        tty.close()


# ---- the command --------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="relay secret", description="set the relay's secrets without showing them")
    p.add_argument("--env-file", default=os.environ.get("RELAY_ENV_FILE", ".env"),
                   help="the secrets file (default: $RELAY_ENV_FILE or .env)")
    sub = p.add_subparsers(dest="action", required=True)
    for action in ("set", "generate", "reveal"):
        sub.add_parser(action).add_argument("name")
    sub.add_parser("list")
    setting = sub.add_parser("setting")
    setting.add_argument("name")
    setting.add_argument("value")
    args = p.parse_args(argv)
    path = Path(args.env_file)
    try:
        if args.action == "list":
            for name, has in sorted(names(path).items()):
                print(f"{name}: {'set' if has else 'empty'}")
            return 0
        if not NAME.match(args.name):
            raise EnvFileError("a name is 2-64 capital letters, digits or '_', starting with a letter")
        if args.action == "setting":
            if any(word in args.name for word in SECRET_WORDS):
                raise EnvFileError(f"{args.name} sounds like a secret: set it with `relay secret set {args.name}` "
                                   f"(typed at the terminal, never on the command line)")
            changed = put(path, args.name, args.value)
            print(f"{args.name}: {'set' if changed else 'unchanged'}")
        elif args.action == "set":
            value = ask_hidden(f"{args.name} (typing is hidden; paste and press Enter): ")
            changed = put(path, args.name, value)
            print(f"{args.name}: {'set' if changed else 'unchanged'} ({len(value)} characters) in {path}. "
                  f"Restart the relay to use it.")
        elif args.action == "generate":
            if get(path, args.name):
                raise EnvFileError(f"{args.name} already has a value: it is not replaced here (set it to replace it)")
            put(path, args.name, secrets.token_urlsafe(GENERATED_BYTES))
            print(f"{args.name}: a new random value is in {path} (not shown; `relay secret reveal {args.name}` "
                  f"shows it on your terminal). Restart the relay to use it.")
        else:
            value = get(path, args.name)
            if not value:
                raise EnvFileError(f"{args.name} is not set in {path}")
            show_on_tty(value)
            print(f"{args.name}: shown on the terminal")
    except EnvFileError as e:
        print(f"relay secret: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
