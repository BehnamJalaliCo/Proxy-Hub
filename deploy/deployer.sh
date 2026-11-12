#!/usr/bin/env bash
# GitOps deployer (systemd timer, every 2 minutes). Deploys the newest commit of DEPLOY_BRANCH;
# if /healthz is not green within the deadline, rolls back to the last good commit.
# Migrations must stay backward compatible (expand/contract), because code can roll back.
set -euo pipefail

# Tests only (panel/tests/test_ops_deployer.py): PROXYHUB_DEPLOY_ROOT puts every host path below
# under a sandbox directory. systemd sets neither variable, so the real paths are used.
ROOT="${PROXYHUB_DEPLOY_ROOT:-}"
ETC="$ROOT/etc/proxy-hub"
STATE="$ROOT/var/lib/proxy-hub"
CHECKOUT="$ROOT/opt/proxy-hub"
SBIN="$ROOT/usr/local/sbin"
HEALTH_URL=http://127.0.0.1:8000/healthz
HEALTH_DEADLINE_S="${PROXYHUB_DEPLOY_HEALTH_DEADLINE_S:-180}"
DNS_NAME='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'

exec 9>"$ROOT/run/proxyhub-deploy.lock"
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

# wait_healthy <up_done>: /healthz answers 200 AND reports a worker heartbeat written after "compose
# up" returned ($SECONDS then). By that time the previous worker container is stopped, so its last
# heartbeat (fresh for 90 s) cannot vouch for a new worker that dies at start. SECONDS counts whole
# seconds and the age is cut to whole seconds, so "age + 2 <= elapsed" is strictly after up_done.
wait_healthy() {
  local up_done="$1" end=$((SECONDS + HEALTH_DEADLINE_S)) body age last="no answer"
  local age_re='"worker_heartbeat_age_s":[[:space:]]*-?([0-9]+)'
  while [ $SECONDS -lt $end ]; do
    if body="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)"; then
      if [[ "$body" =~ $age_re ]]; then
        age=$((10#${BASH_REMATCH[1]}))
        if [ $((age + 2)) -le $((SECONDS - up_done)) ]; then return 0; fi
        last="no worker heartbeat since the deploy yet (last one ${age}s ago)"
      else
        last="no worker heartbeat age in the answer"
      fi
    else
      last="/healthz did not answer 200"
    fi
    sleep 5
  done
  log "health: $last"
  return 1
}

# Called as "if deploy_commit ...", where bash ignores errexit: every step checks its own status,
# so a failed build or pull (e.g. a registry 429) is a failed deploy, never a silent "ok".
# deploy_commit <commit> [rollback]: a rollback goes back to a commit that ran here before, so its
# migrations are applied, and the failed commit may have migrated the database past what it knows:
# its migrate service is not run (scale 0; api and worker then do not wait for it).
deploy_commit() {
  local commit="$1" mode="${2:-deploy}"
  git -C "$CHECKOUT" checkout --quiet --detach "$commit" || return 1
  if [ -f "$CHECKOUT/deploy/ensure_secrets.sh" ]; then bash "$CHECKOUT/deploy/ensure_secrets.sh" || return 1; fi
  export PROXYHUB_GIT_COMMIT="$commit"
  # Caddy serves PANEL_DOMAIN as its site address: a malformed value would crash-loop Caddy while
  # the health check (API on loopback) still passes.
  if [ -n "${PANEL_DOMAIN:-}" ] && ! [[ "$PANEL_DOMAIN" =~ $DNS_NAME ]]; then
    log "PANEL_DOMAIN must be a lower-case DNS name"; return 1
  fi
  if [ -n "${AGENT_DOMAIN:-}" ]; then
    if ! [[ "$AGENT_DOMAIN" =~ $DNS_NAME ]] || [ "$AGENT_DOMAIN" = "${PANEL_DOMAIN:-}" ]; then
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
  if [ "$mode" = rollback ]; then
    compose up -d --remove-orphans --scale migrate=0 || { log "compose up failed"; return 1; }
  else
    compose up -d --remove-orphans || { log "compose up failed"; return 1; }
  fi
  local up_done=$SECONDS
  # Caddy's files are bind-mounted and its admin API is off: restart Caddy when any of them changed.
  if [ -n "${PANEL_DOMAIN:-}" ]; then
    local caddy_hash
    caddy_hash="$(caddy_inputs_hash)"
    if [ "$caddy_hash" != "$(cat "$STATE/caddyfile_hash" 2>/dev/null || echo none)" ]; then
      compose restart caddy || { log "caddy restart failed"; return 1; }
      echo "$caddy_hash" > "$STATE/caddyfile_hash"
    fi
  fi
  wait_healthy "$up_done"
}

# node-exporter textfile: the outcome of the last run that did something (refused by the merge
# guard, failed, deployed). Runs that change nothing leave it alone, so a refusal or a failure stays
# visible (DeployRefused, DeployFailed, DeployRollbackFailed) until a good deploy clears it.
# write_deploy_metric <refused> <failed> <rollback_failed>, each 0 or 1.
write_deploy_metric() {
  local dir="${PROXYHUB_METRICS_DIR:-$STATE/metrics}" tmp
  [ -d "$dir" ] || return 0
  tmp="$(mktemp "$dir/.proxyhub_deploy.prom.XXXXXX")"
  cat >"$tmp" <<PROM
# HELP proxyhub_deploy_refused 1 if the newest commit was refused: not a GitHub-signed PR merge.
# TYPE proxyhub_deploy_refused gauge
proxyhub_deploy_refused $1
# HELP proxyhub_deploy_failed 1 if the newest commit failed to deploy (build or health check).
# TYPE proxyhub_deploy_failed gauge
proxyhub_deploy_failed $2
# HELP proxyhub_deploy_rollback_failed 1 if the rollback after that failure failed too.
# TYPE proxyhub_deploy_rollback_failed gauge
proxyhub_deploy_rollback_failed $3
# HELP proxyhub_deploy_guard_timestamp_seconds When the deployer last recorded an outcome.
# TYPE proxyhub_deploy_guard_timestamp_seconds gauge
proxyhub_deploy_guard_timestamp_seconds $(date +%s)
PROM
  chmod 0644 "$tmp"
  mv -f "$tmp" "$dir/proxyhub_deploy.prom"
}

# A failed (or refused) commit is recorded with the deploy.env it was tried with.
record_failed() {
  echo "$target" > "$STATE/failed_commit"
  echo "$cfg_hash" > "$STATE/failed_config_hash"
}

# Every deploy builds and tags proxyhub/panel:<commit>, and nothing else removes old ones: keep the
# image now running and the rollback target, remove the other tags. No -f: Docker refuses to remove
# an image that a container still uses. A failed cleanup never fails a deploy.
prune_old_images() {
  local keep="$1" previous="$2" tags tag
  tags="$(docker image ls proxyhub/panel --format '{{.Tag}}')" || return 1
  for tag in $tags; do
    case "$tag" in
      "$keep" | "$previous" | "<none>") continue ;;
    esac
    docker image rm "proxyhub/panel:$tag" >/dev/null 2>&1 || log "kept image proxyhub/panel:${tag:0:12} (in use)"
  done
}

git -C "$CHECKOUT" fetch --quiet origin "$DEPLOY_BRANCH"
target="$(git -C "$CHECKOUT" rev-parse "origin/$DEPLOY_BRANCH")"
current="$(cat "$STATE/deployed_commit" 2>/dev/null || echo none)"
# deploy.env changes (e.g. PANEL_DOMAIN) must also be applied, not only new commits.
cfg_hash="$(sha256sum "$ETC/deploy.env" | cut -d" " -f1)"
deployed_cfg="$(cat "$STATE/deployed_config_hash" 2>/dev/null || echo none)"
failed="$(cat "$STATE/failed_commit" 2>/dev/null || echo none)"
# State written by an older deployer has no failed_config_hash: assume the deployed deploy.env.
failed_cfg="$(cat "$STATE/failed_config_hash" 2>/dev/null || echo "$deployed_cfg")"

if [ "${FORCE:-0}" != 1 ]; then
  [ "$target" = "$current" ] && [ "$cfg_hash" = "$deployed_cfg" ] && exit 0
  # No retry of a known-bad commit; a deploy.env edit (it can be the cause) gives it one new try.
  [ "$target" = "$failed" ] && [ "$cfg_hash" = "$failed_cfg" ] && exit 0
fi

# Protected main without GitHub Pro (D31/D36): from main, only pull-request merges made and signed
# by GitHub are deployed (deploy/verify_main.sh, installed copy from the last good deploy). A commit
# pushed straight to main is refused, recorded as failed (no retry) and raised as an alert.
if [ "$DEPLOY_BRANCH" = main ] || [ "${REQUIRE_GITHUB_MERGE:-0}" = 1 ]; then
  guard="$SBIN/proxyhub-verify-main"
  if [ -x "$guard" ] && "$guard" "$CHECKOUT" "$current" "$target" "$ETC/github-web-flow.gpg"; then
    :
  else
    record_failed
    write_deploy_metric 1 0 0
    # Still fail closed without the guard or its key, but name that cause: "not a signed merge"
    # would send an operator looking for a push to main that never happened.
    if [ ! -x "$guard" ]; then
      log "REFUSED: the merge guard $guard is not installed (run deploy/install.sh); nothing deployed"
    elif [ ! -s "$ETC/github-web-flow.gpg" ]; then
      log "REFUSED: GitHub's signing key $ETC/github-web-flow.gpg is missing (run deploy/install.sh); nothing deployed"
    else
      log "REFUSED: $target is not a GitHub-signed pull-request merge on $DEPLOY_BRANCH; nothing deployed"
    fi
    exit 1
  fi
fi

log "deploying $target (current: $current)"
if deploy_commit "$target"; then
  echo "$target" > "$STATE/deployed_commit"
  echo "$cfg_hash" > "$STATE/deployed_config_hash"
  rm -f "$STATE/failed_commit" "$STATE/failed_config_hash"
  write_deploy_metric 0 0 0
  install -m 0755 "$CHECKOUT/deploy/deployer.sh" "$SBIN/proxyhub-deploy"
  install -m 0755 "$CHECKOUT/deploy/verify_main.sh" "$SBIN/proxyhub-verify-main"
  install -m 0644 "$CHECKOUT/deploy/known_hosts.github" "$ETC/known_hosts"
  # The Claude bridge (D52), only where P22 installed it: refresh its copy from this good commit.
  # Never fails a deploy: the panel falls back to the content bank while the bridge is away.
  if [ -f "$CHECKOUT/deploy/claude-bridge/install.sh" ]; then
    bash "$CHECKOUT/deploy/claude-bridge/install.sh" --update || log "claude bridge refresh failed"
  fi
  prune_old_images "$target" "$current" || log "old image cleanup failed"
  log "ok: $target is healthy"
else
  record_failed
  log "FAILED: $target not healthy within ${HEALTH_DEADLINE_S}s"
  compose logs --no-color --tail 80 api worker migrate 2>&1 | sed 's/^/  | /' || true
  rollback_failed=0
  if [ "$current" != none ]; then
    log "rolling back to $current"
    if deploy_commit "$current" rollback; then
      # It runs with this deploy.env now: an edit of deploy.env cannot be rolled back.
      echo "$cfg_hash" > "$STATE/deployed_config_hash"
      if [ "$current" = "$target" ]; then
        # Only deploy.env changed and the second try is healthy: that is a good deploy.
        rm -f "$STATE/failed_commit" "$STATE/failed_config_hash"
        write_deploy_metric 0 0 0
        log "ok: $target is healthy on the second try"
        exit 0
      fi
      log "rollback ok"
    else
      log "ROLLBACK FAILED"
      rollback_failed=1
    fi
  fi
  write_deploy_metric 0 1 "$rollback_failed"
  exit 1
fi
