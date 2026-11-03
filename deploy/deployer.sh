#!/usr/bin/env bash
# GitOps deployer (systemd timer, every 2 minutes). Deploys the newest commit of DEPLOY_BRANCH;
# if /healthz is not green within the deadline, rolls back to the last good commit.
# Migrations must stay backward compatible (expand/contract), because code can roll back.
set -euo pipefail

ETC=/etc/proxy-hub
STATE=/var/lib/proxy-hub
CHECKOUT=/opt/proxy-hub
HEALTH_URL=http://127.0.0.1:8000/healthz
HEALTH_DEADLINE_S=180

exec 9>/run/proxyhub-deploy.lock
flock -n 9 || exit 0

# shellcheck source=/dev/null
source "$ETC/deploy.env"
export GIT_SSH_COMMAND="ssh -i $ETC/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

log() { echo "proxyhub-deploy: $*"; }

compose() {
  local profiles=""
  [ -n "${PANEL_DOMAIN:-}" ] && profiles="edge"
  COMPOSE_PROFILES="$profiles" PANEL_DOMAIN="${PANEL_DOMAIN:-}" \
    docker compose -p proxyhub -f "$CHECKOUT/deploy/docker-compose.yml" "$@"
}

wait_healthy() {
  local end=$((SECONDS + HEALTH_DEADLINE_S))
  while [ $SECONDS -lt $end ]; do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then return 0; fi
    sleep 5
  done
  return 1
}

deploy_commit() {
  local commit="$1"
  git -C "$CHECKOUT" checkout --quiet --detach "$commit"
  export PROXYHUB_GIT_COMMIT="$commit"
  compose build --quiet
  compose up -d --remove-orphans
  wait_healthy
}

git -C "$CHECKOUT" fetch --quiet origin "$DEPLOY_BRANCH"
target="$(git -C "$CHECKOUT" rev-parse "origin/$DEPLOY_BRANCH")"
current="$(cat "$STATE/deployed_commit" 2>/dev/null || echo none)"
failed="$(cat "$STATE/failed_commit" 2>/dev/null || echo none)"

if [ "${FORCE:-0}" != 1 ]; then
  [ "$target" = "$current" ] && exit 0
  [ "$target" = "$failed" ] && exit 0   # do not retry a known-bad commit every 2 minutes
fi

log "deploying $target (current: $current)"
if deploy_commit "$target"; then
  echo "$target" > "$STATE/deployed_commit"
  rm -f "$STATE/failed_commit"
  install -m 0755 "$CHECKOUT/deploy/deployer.sh" /usr/local/sbin/proxyhub-deploy
  log "ok: $target is healthy"
else
  echo "$target" > "$STATE/failed_commit"
  log "FAILED: $target not healthy within ${HEALTH_DEADLINE_S}s"
  compose logs --no-color --tail 80 api worker migrate 2>&1 | sed 's/^/  | /' || true
  if [ "$current" != none ]; then
    log "rolling back to $current"
    if deploy_commit "$current"; then log "rollback ok"; else log "ROLLBACK FAILED"; fi
  fi
  exit 1
fi
