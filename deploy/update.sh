#!/usr/bin/env bash
# relay update [<version>] [--yes] [--no-backup]: move the relay to a new version (default: the
# newest release tag). Without --yes it only says what it would do. With it: a copy of the database
# (it stops if none can be made, unless --no-backup: Postgres is copied with pg_dump first), the new
# code, the relay command and the containers made again, and a wait until the relay answers.
# Your .env and config/ stay as they are.
set -euo pipefail
[ -r /etc/default/quanti-relay ] && . /etc/default/quanti-relay
DIR="${RELAY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ROOT="${RELAY_INSTALL_ROOT:-}"      # tests only: where /usr/local/bin is
cd "$DIR"
YES=0; BACKUP=1; TARGET=""
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --no-backup) BACKUP=0 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) TARGET="$arg" ;;
  esac
done
say() { echo "[update] $*"; }

# Tags as the repository has them now (a moved tag too), and its branches as origin/<name>
git fetch --quiet --prune --force --tags origin '+refs/heads/*:refs/remotes/origin/*'
if [ -z "$TARGET" ]; then
  TARGET="$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
  [ -n "$TARGET" ] || { say "there is no release tag yet: name the version (a tag or branch)"; exit 2; }
fi
# A tag as it is; a branch as the repository has it now, never a stale local copy
if git rev-parse --verify --quiet "refs/tags/$TARGET^{commit}" >/dev/null; then
  REF="refs/tags/$TARGET"
elif git rev-parse --verify --quiet "refs/remotes/origin/$TARGET^{commit}" >/dev/null; then
  REF="refs/remotes/origin/$TARGET"
else
  say "unknown version: $TARGET"; exit 2
fi
CURRENT="$(git describe --tags --always)"
say "now: $CURRENT  ->  $TARGET ($(git rev-parse --short "$REF^{commit}"))"
git log --oneline "HEAD..$REF" 2>/dev/null | head -n 20 || true
if [ "$YES" -ne 1 ]; then
  say "nothing changed. To update: sudo relay update $TARGET --yes"
  exit 0
fi

if [ "$BACKUP" -eq 1 ]; then
  say "copying the database first"
  docker compose --profile tls exec -T relay relay db backup || {
    say "STOP: no copy of the database could be made. Postgres: copy it with pg_dump, then run this again"
    say "with --no-backup. SQLite: see the message above (disk full? the relay not running?)"
    exit 1
  }
fi
git -c advice.detachedHead=false checkout --quiet "$REF^{commit}"
install -m 755 "$DIR/deploy/relay" "$ROOT/usr/local/bin/relay"     # the relay command of this version
say "rebuilding and starting"
# Made again, the proxy too: it reads its Caddyfile only when it starts
docker compose --profile tls up -d --build --force-recreate
for _ in $(seq 1 36); do
  if docker compose --profile tls exec -T relay python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)" >/dev/null 2>&1; then
    say "done: $TARGET runs and answers"
    exit 0
  fi
  sleep 5
done
say "the relay does not answer after the update. See: relay logs"
say "to go back: sudo relay update $CURRENT --yes (the database copy is under the relay's data volume, backups/)"
exit 1
