#!/bin/bash
# Paste this as the new server's "user data" (also called cloud-init or startup script) when you
# create it, with VERSION filled in below (docs/cloud.md). The server then sets the
# relay up by itself on its first start. Safe to run again (some clouds run it at every start).
set -euo pipefail
REPOSITORY="https://github.com/Quantitizer/quanti-relay.git"   # the relay's git address (your own copy's, if you use one)
VERSION="<VERSION>"                 # the release to install, e.g. a tag
OPTIONS=""                          # with your own domain: "--domain <DOMAIN>" (its DNS points here)
apt-get -o DPkg::Lock::Timeout=600 update -q            # the server may be updating itself at first boot
apt-get -o DPkg::Lock::Timeout=600 install -y -q git
[ -d /opt/quanti-relay/.git ] || git clone --branch "$VERSION" "$REPOSITORY" /opt/quanti-relay
# shellcheck disable=SC2086  # OPTIONS is a list of words
/opt/quanti-relay/deploy/install.sh $OPTIONS > /var/log/quanti-relay-install.log 2>&1
