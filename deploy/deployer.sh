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

# The node agents' mTLS site (deploy/caddy/agent-on.caddy) is served only when AGENT_DOMAIN is set;
# otherwise Caddy imports agent-off.caddy and asks for no certificate for it.
agent_site() {
  if [ -n "${AGENT_DOMAIN:-}" ]; then echo on; else echo off; fi
}

compose() {
  local profiles=""
  [ -n "${PANEL_DOMAIN:-}" ] && profiles="edge"
  COMPOSE_PROFILES="$profiles" PANEL_DOMAIN="${PANEL_DOMAIN:-}" AGENT_DOMAIN="${AGENT_DOMAIN:-}" \
    PROXYHUB_AGENT_SITE="$(agent_site)" \
    docker compose -p proxyhub -f "$CHECKOUT/deploy/docker-compose.yml" "$@"
}

# The PUBLIC agent CA certificate for Caddy's client-certificate check, derived from the agent CA
# key inside the freshly built panel image (python -m proxyhub.pki ca-pem; the key never leaves
# the container). Written atomically, 0644, only if it is one valid certificate.
AGENT_TRUST_DIR="$STATE/caddy-agent"
export_agent_ca() {
  local tmp
  install -d -m 0755 "$AGENT_TRUST_DIR"
  tmp="$(mktemp "$AGENT_TRUST_DIR/.agent_ca.XXXXXX")"
  if ! compose run --rm --no-deps -T api python -m proxyhub.pki ca-pem > "$tmp"; then
    rm -f "$tmp"; log "agent CA export failed"; return 1
  fi
  if [ "$(grep -c -- '-----BEGIN CERTIFICATE-----' "$tmp")" != 1 ] || ! openssl x509 -noout -in "$tmp" 2>/dev/null; then
    rm -f "$tmp"; log "agent CA export is not one certificate"; return 1
  fi
  chmod 0644 "$tmp"
  mv -f "$tmp" "$AGENT_TRUST_DIR/agent_ca.pem"
}

# Everything Caddy reads from files: a change in any of them needs a Caddy restart (admin API off).
caddy_inputs_hash() {
  cat "$CHECKOUT/deploy/Caddyfile" "$CHECKOUT"/deploy/caddy/*.caddy "$AGENT_TRUST_DIR/agent_ca.pem" 2>/dev/null \
    | sha256sum | cut -d" " -f1
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
  if [ -n "${AGENT_DOMAIN:-}" ]; then
    if ! [[ "$AGENT_DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
      || [ "$AGENT_DOMAIN" = "${PANEL_DOMAIN:-}" ]; then
      log "AGENT_DOMAIN must be a lower-case DNS name other than PANEL_DOMAIN"; return 1
    fi
    # Without PANEL_DOMAIN Caddy (edge profile) does not run, so no node could ever register and a
    # test node would only burn money until its boot timeout.
    if [ -z "${PANEL_DOMAIN:-}" ]; then
      log "AGENT_DOMAIN needs PANEL_DOMAIN (Caddy serves the agent site)"; return 1
    fi
  fi
  compose build --quiet || { log "compose build failed"; return 1; }
  if [ -n "${AGENT_DOMAIN:-}" ]; then export_agent_ca || return 1; fi
  compose up -d --remove-orphans || { log "compose up failed"; return 1; }
  # Caddy's files are bind-mounted and its admin API is off: restart Caddy when any of them changed.
  if [ -n "${PANEL_DOMAIN:-}" ]; then
    local caddy_hash
    caddy_hash="$(caddy_inputs_hash)"
    if [ "$caddy_hash" != "$(cat "$STATE/caddyfile_hash" 2>/dev/null || echo none)" ]; then
      compose restart caddy || { log "caddy restart failed"; return 1; }
      echo "$caddy_hash" > "$STATE/caddyfile_hash"
    fi
  fi
  wait_healthy
}

# node-exporter textfile: 1 while the newest commit of main was refused by the merge guard.
write_guard_metric() {
  local dir="${PROXYHUB_METRICS_DIR:-/var/lib/proxy-hub/metrics}" tmp
  [ -d "$dir" ] || return 0
  tmp="$(mktemp "$dir/.proxyhub_deploy.prom.XXXXXX")"
  cat >"$tmp" <<PROM
# HELP proxyhub_deploy_refused 1 if the newest commit was refused: not a GitHub-signed PR merge.
# TYPE proxyhub_deploy_refused gauge
proxyhub_deploy_refused $1
# HELP proxyhub_deploy_guard_timestamp_seconds When the merge guard last ran.
# TYPE proxyhub_deploy_guard_timestamp_seconds gauge
proxyhub_deploy_guard_timestamp_seconds $(date +%s)
PROM
  chmod 0644 "$tmp"
  mv -f "$tmp" "$dir/proxyhub_deploy.prom"
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

# Protected main without GitHub Pro (D31/D36): from main, only pull-request merges made and signed
# by GitHub are deployed (deploy/verify_main.sh, installed copy from the last good deploy). A commit
# pushed straight to main is refused, recorded as failed (no retry) and raised as an alert.
if [ "$DEPLOY_BRANCH" = main ] || [ "${REQUIRE_GITHUB_MERGE:-0}" = 1 ]; then
  guard=/usr/local/sbin/proxyhub-verify-main
  if [ -x "$guard" ] && "$guard" "$CHECKOUT" "$current" "$target" "$ETC/github-web-flow.gpg"; then
    write_guard_metric 0
  else
    echo "$target" > "$STATE/failed_commit"
    write_guard_metric 1
    log "REFUSED: $target is not a GitHub-signed pull-request merge on $DEPLOY_BRANCH; nothing deployed"
    exit 1
  fi
fi

log "deploying $target (current: $current)"
if deploy_commit "$target"; then
  echo "$target" > "$STATE/deployed_commit"
  echo "$cfg_hash" > "$STATE/deployed_config_hash"
  rm -f "$STATE/failed_commit"
  install -m 0755 "$CHECKOUT/deploy/deployer.sh" /usr/local/sbin/proxyhub-deploy
  install -m 0755 "$CHECKOUT/deploy/verify_main.sh" /usr/local/sbin/proxyhub-verify-main
  install -m 0644 "$CHECKOUT/deploy/known_hosts.github" "$ETC/known_hosts"
  # The Claude bridge (D52), only where P22 installed it: refresh its copy from this good commit.
  # Never fails a deploy: the panel falls back to the content bank while the bridge is away.
  if [ -f "$CHECKOUT/deploy/claude-bridge/install.sh" ]; then
    bash "$CHECKOUT/deploy/claude-bridge/install.sh" --update || log "claude bridge refresh failed"
  fi
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
