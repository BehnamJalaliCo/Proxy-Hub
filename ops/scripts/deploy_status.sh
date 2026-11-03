#!/usr/bin/env bash
# Deployment status for prompts: deployed commit vs. the branch head on GitHub, failed commit,
# and panel health. Fetches with the deployer's own read-only deploy key and pinned GitHub host keys
# (a plain "git fetch" in /opt/proxy-hub fails: root has no key for it; found in P06).
# Prints no secret. Usage: sudo bash ops/scripts/deploy_status.sh
set -euo pipefail

ETC=/etc/proxy-hub
STATE=/var/lib/proxy-hub
CHECKOUT=/opt/proxy-hub

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
branch="$(grep -E '^DEPLOY_BRANCH=' "$ETC/deploy.env" | cut -d= -f2-)"
[ -n "$branch" ] || { echo "DEPLOY_BRANCH is not set in $ETC/deploy.env"; exit 1; }

export GIT_SSH_COMMAND="ssh -i $ETC/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$ETC/known_hosts"
git -C "$CHECKOUT" fetch --quiet origin "$branch"
origin="$(git -C "$CHECKOUT" rev-parse "origin/$branch")"
deployed="$(cat "$STATE/deployed_commit" 2>/dev/null || echo none)"
failed="$(cat "$STATE/failed_commit" 2>/dev/null || echo none)"
health="$(curl -fsS --max-time 5 http://127.0.0.1:8000/healthz 2>/dev/null || echo unreachable)"

echo "branch:          $branch"
echo "origin commit:   $origin"
echo "deployed commit: $deployed"
echo "failed commit:   $failed"
echo "health:          $health"
if [ "$origin" = "$deployed" ]; then
  echo "RESULT: up to date"
elif [ "$origin" = "$failed" ]; then
  echo "RESULT: the newest commit FAILED to deploy (see: journalctl -u proxyhub-deployer.service -n 60)"
else
  echo "RESULT: newer commit not deployed yet (the deployer runs every 2 minutes)"
fi
