#!/usr/bin/env bash
# Sets the relay up on a fresh Ubuntu (22.04, 24.04) or Debian 13 server, the same way on any cloud
# (docs/cloud.md).
#
#   sudo deploy/install.sh                          HTTPS on the server's own IP address (free)
#   sudo deploy/install.sh --domain relay.example.com   HTTPS on your own domain (point it here first)
#   sudo deploy/install.sh --sslip                  HTTPS on <ip>.sslip.io (a free name, if a sender
#                                                   will not take an IP address)
#
# Run it from inside the relay's folder: a git clone made by root (sudo git clone ... /opt/quanti-relay),
# since root runs its scripts later. It is safe to run again: what is there already is kept (the API
# key above all), and the rest is brought up to date.
#
# What it does: installs Docker, turns on automatic security updates and a firewall (SSH, 80, 443 over IPv4),
# allows SSH with keys only (no passwords),
# writes .env (a new API key that is never printed, paper trading, the proxy's address), starts the
# relay behind the TLS proxy (compose profile "tls"), installs the `relay` command and waits until
# the relay answers over HTTPS. It never sets an exchange key and never switches to live trading.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="${RELAY_DOMAIN:-}"
PUBLIC_IP="${RELAY_PUBLIC_IP:-}"
SSLIP=0
PROXY="172.30.77.10"                # the TLS proxy's fixed address (compose.yaml): the only one the relay trusts
ROOT="${RELAY_INSTALL_ROOT:-}"      # tests only: where /etc and /usr/local/bin are
OWNER="${RELAY_DIR_OWNER:-0}"       # tests only: who must own the relay's folder (root)
APT="apt-get -o DPkg::Lock::Timeout=600"   # a fresh server updates itself at first boot: wait for it

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?--domain needs a name}"; shift 2 ;;
    --public-ip) PUBLIC_IP="${2:?--public-ip needs an address}"; shift 2 ;;
    --sslip) SSLIP=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { echo "[install] $*"; }
die() { echo "[install] STOP: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run it as root: sudo $0"
command -v apt-get >/dev/null || die "this script knows Ubuntu and Debian (apt). On another system follow docs/cloud.md by hand."
if [ ! -f "$DIR/compose.yaml" ] || [ ! -f "$DIR/relay/envfile.py" ]; then
  die "run it from the relay's folder (a git clone of the repository)"
fi
# root runs the scripts in this folder (the relay command, updates): nobody else may be able to change them
other="$(find "$DIR" \( -path "$DIR/config" -o -path "$DIR/.env" \) -prune -o \( ! -user "$OWNER" -o -perm -0002 \) -print -quit)"
[ -z "$other" ] || die "$other is not root's (or anyone may write it): clone the relay as root, e.g. sudo git clone https://github.com/Quantitizer/quanti-relay.git /opt/quanti-relay"
if [ -n "$DOMAIN" ] && ! [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ && "$DOMAIN" == *.* ]]; then
  die "--domain $DOMAIN is not a domain name"
fi

# ---- packages, updates, firewall -------------------------------------------------------------
say "installing Docker and the system tools"
export DEBIAN_FRONTEND=noninteractive
$APT update -q
$APT install -y -q ca-certificates curl git python3 ufw unattended-upgrades docker.io
if ! docker compose version >/dev/null 2>&1; then
  # Ubuntu: docker-compose-v2; Debian 13: docker-compose; Docker's own packages: docker-compose-plugin
  for pkg in docker-compose-v2 docker-compose docker-compose-plugin; do
    $APT install -y -q "$pkg" >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && break
  done
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 could not be installed (Debian 12 has none): install Docker from docs.docker.com/engine/install, then run this again"
systemctl enable --now docker >/dev/null

say "turning on automatic security updates"
mkdir -p "$ROOT/etc/apt/apt.conf.d"
cat > "$ROOT/etc/apt/apt.conf.d/20auto-upgrades" <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

say "firewall: SSH, 80 and 443 only"
ufw allow OpenSSH >/dev/null
# SSH on another port too, if the server's SSH listens there: the firewall must not lock you out
for port in $(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}'); do
  [[ "$port" =~ ^[0-9]+$ ]] && ufw allow "$port/tcp" >/dev/null
done
# 80 and 443 over IPv4 only: the relay tells clients apart by address, and IPv6 would reach it
# through Docker's proxy with one shared address for everyone
ufw allow proto tcp from 0.0.0.0/0 to any port 80 >/dev/null
ufw allow proto tcp from 0.0.0.0/0 to any port 443 >/dev/null
ufw --force enable >/dev/null

# A server made without an SSH key takes passwords over SSH from the whole internet, where they can
# be guessed. Keys only from now on; the provider's browser console still takes the password.
say "SSH: keys only (the provider's browser console still signs in with a password)"
mkdir -p "$ROOT/etc/ssh/sshd_config.d"
SSHD_CONF="$ROOT/etc/ssh/sshd_config.d/00-quanti-relay.conf"     # read first: its values win
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' > "$SSHD_CONF"
if sshd -t >/dev/null 2>&1; then
  systemctl try-reload-or-restart ssh >/dev/null 2>&1 || systemctl try-reload-or-restart sshd >/dev/null 2>&1 || true
else
  rm -f "$SSHD_CONF"
  say "WARNING: the SSH settings did not check out, so SSH is left as it was"
fi

# ---- the address -----------------------------------------------------------------------------
if [ -z "$PUBLIC_IP" ]; then
  # The address the internet sees (a cloud server often only knows its private one).
  PUBLIC_IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com 2>/dev/null || curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
  PUBLIC_IP="$(echo "$PUBLIC_IP" | tr -d '[:space:]')"
fi
[[ "$PUBLIC_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "could not tell this server's public IPv4 address: pass --public-ip"

if [ -n "$DOMAIN" ]; then
  HOST="$DOMAIN"; PROFILE="classic"
  resolved="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
  [ "$resolved" = "$PUBLIC_IP" ] || say "WARNING: $DOMAIN points to ${resolved:-nothing}, not to $PUBLIC_IP: set its DNS A record first, or the certificate fails"
elif [ "$SSLIP" -eq 1 ]; then
  HOST="${PUBLIC_IP//./-}.sslip.io"; PROFILE="classic"
else
  HOST="$PUBLIC_IP"; PROFILE="shortlived"     # IP certificates are short-lived; Caddy renews them
fi
say "the relay's address: https://$HOST"

# ---- .env ------------------------------------------------------------------------------------
cd "$DIR"
touch .env
chmod 600 .env
secret() { python3 -I "$DIR/relay/envfile.py" --env-file "$DIR/.env" "$@"; }
secret generate RELAY_API_KEY >/dev/null 2>&1 || true          # kept when there is one already
secret setting RELAY_PUBLIC_HOST "$HOST"
secret setting RELAY_ACME_PROFILE "$PROFILE"
secret setting RELAY_PUBLIC_IP "$PUBLIC_IP"
secret setting RELAY_TRUSTED_PROXIES "$PROXY"
grep -qE '^\s*RELAY_DRY_RUN=' .env || secret setting RELAY_DRY_RUN true    # paper; only the owner changes it

mkdir -p config/deployments config/sources config/sinks
chown -R 10001:10001 config                      # the container's user may write it (the console)

# ---- start -----------------------------------------------------------------------------------
say "building and starting (a few minutes the first time)"
docker compose --profile tls up -d --build

mkdir -p "$ROOT/etc/default" "$ROOT/usr/local/bin"
echo "RELAY_DIR=$DIR" > "$ROOT/etc/default/quanti-relay"
install -m 755 "$DIR/deploy/relay" "$ROOT/usr/local/bin/relay"

say "waiting for https://$HOST/health (the certificate takes up to a minute or two)"
ok=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://$HOST/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done

echo
if [ "$ok" -eq 1 ]; then
  say "done: the relay answers at https://$HOST (paper trading)"
else
  say "the relay does not answer at https://$HOST yet. Check: relay logs; docker compose --profile tls logs caddy;"
  say "the cloud provider's firewall must let 80 and 443 in."
fi
cat <<NEXT

  Console:      https://$HOST/console
  Sign in with: sudo relay secret reveal RELAY_API_KEY     (shown on your terminal only)
  Check it:     sudo relay doctor
  Next steps:   docs/setup-playbook.md (exchange keys: sudo relay secret set <NAME>)
NEXT
