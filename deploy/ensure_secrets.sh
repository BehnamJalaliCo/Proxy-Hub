#!/usr/bin/env bash
# Create any missing panel secret (idempotent; never prints a secret). Run as root.
# Used by install.sh and by the deployer before every deploy, so a commit that needs a new
# secret file never breaks an already-installed server.
set -euo pipefail

SECRETS=/etc/proxy-hub/secrets
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
install -d -m 0700 -o root -g root /etc/proxy-hub "$SECRETS"

# 0444 inside a 0700 root-only directory: containers can read, host users cannot reach.
finish() { chown root:root "$SECRETS/$1"; chmod 0444 "$SECRETS/$1"; }

gen_hex() {
  if [ ! -s "$SECRETS/$1" ]; then
    (umask 077; openssl rand -hex 32 > "$SECRETS/$1")
    echo "generated secret: $1"
  fi
  finish "$1"
}

gen_ec_key() {  # EC P-256 private key (PEM)
  if [ ! -s "$SECRETS/$1" ]; then
    (umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$SECRETS/$1" 2>/dev/null)
    echo "generated secret: $1"
  fi
  finish "$1"
}

gen_hex db_password
gen_hex redis_password
gen_hex master_key        # encrypts tokens stored in the database
gen_ec_key agent_ca_key   # panel CA for node agent mTLS
# Caddy -> API shared secret for /agent/v1: proves a request came through the mTLS agent site,
# so the API may trust the client-certificate fingerprint header Caddy adds.
gen_hex agent_proxy_secret
