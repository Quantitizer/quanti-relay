#!/usr/bin/env bash
# Leak / residue checker. Run before every commit and push; CI runs it too.
#   scripts/hygiene_check.sh            check code and project files (relay/, tests/, scripts/, config/,
#                                       pyproject, alembic.ini, README, YAML)
#   scripts/hygiene_check.sh --release  check every tracked file, no exceptions (public-release gate)
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

MODE="${1:-code}"
fail=0
red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

# Files to scan: tracked + staged, never the gitignored staging area.
if [ "$MODE" = "--release" ]; then
  files=$(git ls-files --cached --others --exclude-standard)
else
  files=$(git ls-files --cached --others --exclude-standard -- relay tests scripts config 'pyproject.toml' 'alembic.ini' 'README*' '*.yaml' '*.yml' | grep -v '^hygiene/' || true)
fi
files=$(echo "$files" | grep -vE '^(LICENSE|uv\.lock|scripts/hygiene_check\.sh|hygiene/residue\.txt)$' | while read -r f; do [ -f "$f" ] && echo "$f"; done)

scan() { # $1 = label, $2 = pattern file
  local label="$1" pf="$2" hits
  # A missing pattern file must FAIL, not pass silently: the check would not run at all.
  [ -f "$pf" ] || { red "FAIL $label — pattern file $pf missing"; fail=1; return 0; }
  [ -z "$files" ] && return 0
  hits=$(grep -vE '^\s*(#|$)' "$pf" | while read -r pat; do
    echo "$files" | xargs grep -nHiE -- "$pat" 2>/dev/null | while IFS= read -r line; do printf '[%s] %s\n' "$pat" "$line"; done
  done)
  if [ -n "$hits" ]; then red "FAIL $label"; echo "$hits" | head -50; n=$(echo "$hits" | wc -l); [ "$n" -gt 50 ] && echo "... ($n hits)"; fail=1
  else green "ok   $label"; fi
}

# 1. Secrets: full git history + every publishable file in the working tree (gitignored files excluded).
if command -v gitleaks >/dev/null; then
  gitleaks git . --no-banner --redact -l warn >/dev/null 2>&1 && green "ok   gitleaks (history)" || { red "FAIL gitleaks (history) — run: gitleaks git . -v"; fail=1; }
  tmp=$(mktemp -d); git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do [ -f "$f" ] && { mkdir -p "$tmp/$(dirname "$f")"; cp "$f" "$tmp/$f"; }; done
  gitleaks dir "$tmp" --no-banner --redact -l warn >/dev/null 2>&1 && green "ok   gitleaks (working tree)" || { red "FAIL gitleaks (working tree) — run: gitleaks dir . -v"; fail=1; }
  rm -rf "$tmp"
else
  red "FAIL gitleaks not installed (brew install gitleaks)"; fail=1
fi

# 2. Private identifiers of the predecessor project (local file, never committed).
if [ -f .hygiene-denylist.local ]; then scan "private denylist" .hygiene-denylist.local
elif [ -n "${CI:-}" ]; then echo "skip private denylist (CI)"
else red "FAIL .hygiene-denylist.local missing"; fail=1; fi

# 3. Strategy residue.
scan "strategy residue" hygiene/residue.txt

# 4. Public IPv4 addresses (loopback / unspecified / RFC 5737 doc ranges allowed) and email addresses.
if [ -n "$files" ]; then
  ips=$(echo "$files" | xargs grep -nHoE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' 2>/dev/null | grep -vE ':(127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.[0-9]+|198\.51\.100\.[0-9]+|203\.0\.113\.[0-9]+|10\.[0-9.]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9.]+|192\.168\.[0-9.]+)$' || true)
  # version-like strings (e.g. 1.2.3.4 in lockfiles) are rare in code; review any hit manually
  [ -n "$ips" ] && { red "FAIL IPv4 addresses"; echo "$ips" | head -20; fail=1; } || green "ok   IPv4 addresses"
  emails=$(echo "$files" | xargs grep -nHoE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' 2>/dev/null | grep -viE '@(example\.(com|org|net)|anthropic\.com)$' || true)
  [ -n "$emails" ] && { red "FAIL email addresses"; echo "$emails" | head -20; fail=1; } || green "ok   email addresses"
fi

[ $fail -eq 0 ] && green "HYGIENE CLEAN ($MODE)" || red "HYGIENE FAILED ($MODE)"
exit $fail
