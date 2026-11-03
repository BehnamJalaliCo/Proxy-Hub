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

# age X25519 identity for backups (M1.7). Only root on the host reads it (0400; no container mounts
# it). backup.sh encrypts to its PUBLIC half (/etc/proxy-hub/backup_age_recipient, not secret);
# restore_test.sh decrypts with the private key. Losing this file makes every backup unreadable.
gen_age_key() {
  if [ ! -s "$SECRETS/$1" ]; then
    if ! command -v age-keygen >/dev/null 2>&1; then
      # Never fail here: the deployer runs this before every deploy. install.sh installs age.
      echo "age is not installed: $1 not generated yet (run deploy/install.sh)"
      return 0
    fi
    (umask 077; age-keygen -o "$SECRETS/$1" 2>/dev/null)
    echo "generated secret: $1"
  fi
  chown root:root "$SECRETS/$1"
  chmod 0400 "$SECRETS/$1"
  if command -v age-keygen >/dev/null 2>&1; then
    local pub
    pub="$(age-keygen -y "$SECRETS/$1")"
    if [ "$(cat /etc/proxy-hub/backup_age_recipient 2>/dev/null || true)" != "$pub" ]; then
      (umask 022; echo "$pub" > /etc/proxy-hub/backup_age_recipient)
    fi
  fi
}

# Optional secret that turns a feature on: created EMPTY (= off) so compose can mount it, never
# overwritten. The owner's value goes in later (deploy/monitoring/README.md).
optional() {
  [ -e "$SECRETS/$1" ] || (umask 077; : > "$SECRETS/$1")
  finish "$1"
}

gen_hex db_password
gen_hex redis_password
gen_hex master_key        # encrypts tokens stored in the database
gen_ec_key agent_ca_key   # panel CA for node agent mTLS
# Caddy -> API shared secret for /agent/v1: proves a request came through the mTLS agent site,
# so the API may trust the client-certificate fingerprint header Caddy adds.
gen_hex agent_proxy_secret
gen_hex grafana_admin_password   # Grafana on 127.0.0.1:3000 (user: admin)
gen_age_key backup_age_key
optional alert_bot_token            # Alertmanager -> owner's Telegram (with alert_chat_id)
optional alert_chat_id
optional healthchecks_watchdog_url  # Alertmanager Watchdog -> healthchecks.io (external watchdog)
