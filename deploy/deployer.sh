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
export GIT_SSH_COMMAND="ssh -i $ETC/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$ETC/known_hosts"

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

# Called as "if deploy_commit ...", where bash ignores errexit: every step checks its own status,
# so a failed build or pull (e.g. a registry 429) is a failed deploy, never a silent "ok".
deploy_commit() {
  local commit="$1"
  git -C "$CHECKOUT" checkout --quiet --detach "$commit" || return 1
  if [ -f "$CHECKOUT/deploy/ensure_secrets.sh" ]; then bash "$CHECKOUT/deploy/ensure_secrets.sh" || return 1; fi
  export PROXYHUB_GIT_COMMIT="$commit"
  compose build --quiet || { log "compose build failed"; return 1; }
  compose up -d --remove-orphans || { log "compose up failed"; return 1; }
  # Caddyfile is bind-mounted and Caddy's admin API is off: restart Caddy when the file changed.
  if [ -n "${PANEL_DOMAIN:-}" ]; then
    local caddy_hash
    caddy_hash="$(sha256sum "$CHECKOUT/deploy/Caddyfile" | cut -d" " -f1)"
    if [ "$caddy_hash" != "$(cat "$STATE/caddyfile_hash" 2>/dev/null || echo none)" ]; then
      compose restart caddy || { log "caddy restart failed"; return 1; }
      echo "$caddy_hash" > "$STATE/caddyfile_hash"
    fi
  fi
  wait_healthy
}

git -C "$CHECKOUT" fetch --quiet origin "$DEPLOY_BRANCH"
target="$(git -C "$CHECKOUT" rev-parse "origin/$DEPLOY_BRANCH")"
current="$(cat "$STATE/deployed_commit" 2>/dev/null || echo none)"
# deploy.env changes (e.g. PANEL_DOMAIN) must also be applied, not only new commits.
cfg_hash="$(sha256sum "$ETC/deploy.env" | cut -d" " -f1)"
deployed_cfg="$(cat "$STATE/deployed_config_hash" 2>/dev/null || echo none)"
failed="$(cat "$STATE/failed_commit" 2>/dev/null || echo none)"

if [ "${FORCE:-0}" != 1 ]; then
  [ "$target" = "$current" ] && [ "$cfg_hash" = "$deployed_cfg" ] && exit 0
  [ "$target" = "$failed" ] && [ "$cfg_hash" = "$deployed_cfg" ] && exit 0  # no retry of a known-bad commit
fi

log "deploying $target (current: $current)"
if deploy_commit "$target"; then
  echo "$target" > "$STATE/deployed_commit"
  echo "$cfg_hash" > "$STATE/deployed_config_hash"
  rm -f "$STATE/failed_commit"
  install -m 0755 "$CHECKOUT/deploy/deployer.sh" /usr/local/sbin/proxyhub-deploy
  install -m 0644 "$CHECKOUT/deploy/known_hosts.github" "$ETC/known_hosts"
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
