#!/usr/bin/env bash
# Proxy-Hub panel installer (idempotent). Run as root on the panel server:
#   sudo bash deploy/install.sh <git-branch>
# - creates /etc/proxy-hub (0700) with generated secrets (never printed),
# - creates a read-only GitHub deploy key and prints ONLY its public half,
# - clones the repo to /opt/proxy-hub and installs the GitOps deployer timer,
# - installs "age" and the daily backup + weekly restore-test timers (M1.7).
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
install -d -m 0700 -o root -g root "$STATE/backups"   # encrypted backups (deploy/backup/backup.sh)
install -d -m 0755 -o root -g root "$STATE/metrics"   # node-exporter textfile metrics (no secrets)

# age encrypts backups; ensure_secrets.sh needs age-keygen for the backup key (Ubuntu package).
if ! command -v age >/dev/null || ! command -v age-keygen >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends age
fi

bash "$(dirname "$0")/ensure_secrets.sh"

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

# Pin GitHub host keys (official fingerprints) instead of trust-on-first-use.
install -m 0644 -o root -g root "$(dirname "$0")/known_hosts.github" "$ETC/known_hosts"

if [ ! -f "$ETC/deploy_key" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "proxy-hub-panel-deploy (read-only)" -f "$ETC/deploy_key"
  chmod 0600 "$ETC/deploy_key"
fi

export GIT_SSH_COMMAND="ssh -i $ETC/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$ETC/known_hosts"
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
  git clone --quiet --branch "$BRANCH" "$REPO_SSH" "$CHECKOUT"
fi
# Always install from the requested branch, also when the checkout already existed.
git -C "$CHECKOUT" fetch --quiet origin "$BRANCH"
git -C "$CHECKOUT" checkout --quiet --detach "origin/$BRANCH"

install -m 0755 "$CHECKOUT/deploy/deployer.sh" /usr/local/sbin/proxyhub-deploy
install -m 0644 "$CHECKOUT/deploy/systemd/proxyhub-deployer.service" /etc/systemd/system/
install -m 0644 "$CHECKOUT/deploy/systemd/proxyhub-deployer.timer" /etc/systemd/system/
for unit in proxyhub-backup.service proxyhub-backup.timer proxyhub-restore-test.service proxyhub-restore-test.timer; do
  install -m 0644 "$CHECKOUT/deploy/systemd/$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now proxyhub-deployer.timer
systemctl enable --now proxyhub-backup.timer proxyhub-restore-test.timer

echo "first deploy (this builds images; it can take a few minutes)..."
FORCE=1 /usr/local/sbin/proxyhub-deploy
echo "installed. Deployed commit: $(cat "$STATE/deployed_commit")"
