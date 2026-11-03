#!/usr/bin/env bash
# Proxy-Hub panel installer (idempotent). Run as root on the panel server:
#   sudo bash deploy/install.sh <git-branch>
# - creates /etc/proxy-hub (0700) with generated secrets (never printed),
# - creates a read-only GitHub deploy key and prints ONLY its public half,
# - clones the repo to /opt/proxy-hub and installs the GitOps deployer timer.
set -euo pipefail

BRANCH="${1:-main}"
REPO_SSH="git@github.com:BehnamJalaliCo/Proxy-Hub.git"
ETC=/etc/proxy-hub
SECRETS="$ETC/secrets"
STATE=/var/lib/proxy-hub
CHECKOUT=/opt/proxy-hub

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
command -v docker >/dev/null || { echo "docker is not installed"; exit 1; }
docker compose version >/dev/null || { echo "docker compose plugin is not installed"; exit 1; }

install -d -m 0700 -o root -g root "$ETC" "$SECRETS"
install -d -m 0755 -o root -g root "$STATE"

gen_secret() {  # 0444 inside a 0700 root-only directory: containers can read, host users cannot reach.
  local f="$SECRETS/$1"
  if [ ! -s "$f" ]; then
    umask 077
    openssl rand -hex 32 > "$f"
    echo "generated secret: $1"
  fi
  chown root:root "$f"; chmod 0444 "$f"
}
gen_secret db_password
gen_secret redis_password

if [ ! -f "$ETC/deploy.env" ]; then
  cat > "$ETC/deploy.env" <<CONF
# Non-secret deploy settings. Edit with care; the deployer reads this every run.
DEPLOY_BRANCH=$BRANCH
# Set when DNS for the panel points here, e.g. panel.example.com. Empty = no public edge (Caddy off).
PANEL_DOMAIN=
CONF
  chmod 0600 "$ETC/deploy.env"
else
  sed -i "s/^DEPLOY_BRANCH=.*/DEPLOY_BRANCH=$BRANCH/" "$ETC/deploy.env"
fi

if [ ! -f "$ETC/deploy_key" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "proxy-hub-panel-deploy (read-only)" -f "$ETC/deploy_key"
  chmod 0600 "$ETC/deploy_key"
fi

export GIT_SSH_COMMAND="ssh -i $ETC/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
if ! git ls-remote "$REPO_SSH" >/dev/null 2>&1; then
  echo
  echo "=== ACTION NEEDED (owner) ==="
  echo "Add this PUBLIC key as a read-only Deploy key (Allow write access: OFF):"
  echo "  https://github.com/BehnamJalaliCo/Proxy-Hub/settings/keys/new"
  echo
  cat "$ETC/deploy_key.pub"
  echo
  echo "Then run this installer again."
  exit 2
fi

if [ ! -d "$CHECKOUT/.git" ]; then
  git clone --quiet "$REPO_SSH" "$CHECKOUT"
fi

install -m 0755 "$CHECKOUT/deploy/deployer.sh" /usr/local/sbin/proxyhub-deploy
install -m 0644 "$CHECKOUT/deploy/systemd/proxyhub-deployer.service" /etc/systemd/system/
install -m 0644 "$CHECKOUT/deploy/systemd/proxyhub-deployer.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now proxyhub-deployer.timer

echo "first deploy (this builds images; it can take a few minutes)..."
FORCE=1 /usr/local/sbin/proxyhub-deploy
echo "installed. Deployed commit: $(cat "$STATE/deployed_commit")"
