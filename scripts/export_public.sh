#!/usr/bin/env bash
# Makes the public repository's first commit in a new folder: the files tracked at HEAD, without
# the private history, as one commit with a release tag. It never pushes: creating the public
# repository and pushing to it is the owner's step.
#
#   scripts/export_public.sh <new folder> <version> "<Name> <email>"
#   e.g. scripts/export_public.sh ../quanti-relay-public v0.1.0 "<NAME> <YOUR GITHUB NOREPLY ADDRESS>"
#
# The author is given on purpose: a public commit shows its author's email to everyone, so use the
# GitHub noreply address (github.com/settings/emails), not a private one.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
OUT="${1:?usage: scripts/export_public.sh <new folder> <version> \"<Name> <email>\"}"
VERSION="${2:?give the version, e.g. v0.1.0}"
AUTHOR="${3:?give the author: \"<Name> <your GitHub noreply address>\"}"

die() { echo "[export] STOP: $*" >&2; exit 1; }
say() { echo "[export] $*"; }

[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "$VERSION is not a version like v0.1.0"
[[ "$AUTHOR" =~ ^[^\<\>]+\ \<[^\<\>@\ ]+@[^\<\>@\ ]+\>$ ]] || die "the author must read \"Name <email>\""
[[ "$AUTHOR" == *"@users.noreply.github.com>" ]] || say "WARNING: $AUTHOR is not a GitHub noreply address: it will be public"
[ -z "$(git status --porcelain)" ] || die "commit or stash your changes first: the export is HEAD"
[ ! -e "$OUT" ] || die "$OUT exists already: name a new folder"

say "the release check over every tracked file"
scripts/hygiene_check.sh --release || die "the release check failed: fix what it names first"

mkdir -p "$OUT"
git archive --format=tar HEAD | tar -x -C "$OUT"
cd "$OUT"
git init -q -b main
git add -A
NAME="${AUTHOR% <*}"; EMAIL="${AUTHOR##*<}"; EMAIL="${EMAIL%>}"
export GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL"
git commit -q -m "Quanti Relay $VERSION"
git tag -a "$VERSION" -m "Quanti Relay $VERSION"

say "secrets in the new repository"
gitleaks git . --no-banner --redact -l warn >/dev/null 2>&1 || die "gitleaks found something in $OUT: run gitleaks git $OUT -v"
[ ! -e _import ] && [ ! -e .hygiene-denylist.local ] && [ ! -e .env ] || die "a private file is in $OUT"

say "done: $OUT holds one commit ($(git rev-parse --short HEAD)) tagged $VERSION. Nothing was pushed."
