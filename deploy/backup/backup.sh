#!/usr/bin/env bash
# Daily encrypted backup of the panel database (M1.7). Run as root by proxyhub-backup.timer.
#
#   pg_dump (custom format, compressed) inside the running postgres container, from one snapshot
#   -> age, encrypted to the PUBLIC key /etc/proxy-hub/backup_age_recipient (ensure_secrets.sh)
#   -> /var/lib/proxy-hub/backups/proxyhub-<UTC stamp>.dump.age
#      + proxyhub-<UTC stamp>.manifest.age (row count of every table, same snapshot; restore_test.sh
#        checks a restore against it). The newest 14 backups are kept.
# Plaintext never touches the host disk. Status goes to node-exporter's textfile directory
# (alerts BackupFailed / BackupTooOld). If /etc/proxy-hub/secrets/healthchecks_url is non-empty,
# healthchecks.io is pinged (success or /fail); the URL is never printed.
#
# Overrides (tests, other compose projects): PROXYHUB_COMPOSE_PROJECT (default proxyhub),
# PROXYHUB_BACKUP_DIR, PROXYHUB_METRICS_DIR, PROXYHUB_BACKUP_KEEP, PROXYHUB_BACKUP_RECIPIENT.
set -euo pipefail

PROJECT="${PROXYHUB_COMPOSE_PROJECT:-proxyhub}"
BACKUP_DIR="${PROXYHUB_BACKUP_DIR:-/var/lib/proxy-hub/backups}"
METRICS_DIR="${PROXYHUB_METRICS_DIR:-/var/lib/proxy-hub/metrics}"
KEEP="${PROXYHUB_BACKUP_KEEP:-14}"
RECIPIENT_FILE="${PROXYHUB_BACKUP_RECIPIENT:-/etc/proxy-hub/backup_age_recipient}"
HC_URL_FILE=/etc/proxy-hub/secrets/healthchecks_url
HERE="$(cd "$(dirname "$0")" && pwd)"
DB_USER=proxyhub
DB_NAME=proxyhub
ATTEMPTS=3
RETRY_PAUSE_S="${PROXYHUB_BACKUP_RETRY_PAUSE_S:-120}"

log() { echo "proxyhub-backup: $*"; }

[ "$(id -u)" -eq 0 ] || { log "run as root"; exit 1; }
command -v age >/dev/null || { log "age is not installed (deploy/install.sh installs it)"; exit 1; }
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || { log "PROXYHUB_BACKUP_KEEP must be a positive integer"; exit 1; }

install -d -m 0700 -o root -g root "$BACKUP_DIR"
install -d -m 0755 -o root -g root "$METRICS_DIR"
exec 9>"$BACKUP_DIR/.lock"
flock -n 9 || { log "another backup is running"; exit 1; }

started="$(date +%s)"
ok=0
size=0
tables=0
rows=0
tmp_dump=""
tmp_manifest=""
pg=""
manifest_in_container=""

write_metrics() {
  local newest last_success=0 last_size=0 count tmp
  newest="$(find "$BACKUP_DIR" -maxdepth 1 -name 'proxyhub-*.dump.age' -printf '%f\n' | sort | tail -1)"
  if [ -n "$newest" ]; then
    last_success="$(stat -c %Y "$BACKUP_DIR/$newest")"
    last_size="$(stat -c %s "$BACKUP_DIR/$newest")"
  fi
  count="$(find "$BACKUP_DIR" -maxdepth 1 -name 'proxyhub-*.dump.age' | wc -l)"
  tmp="$(mktemp "$METRICS_DIR/.proxyhub_backup.prom.XXXXXX")"
  cat >"$tmp" <<PROM
# HELP proxyhub_backup_last_run_timestamp_seconds When backup.sh last finished (success or not).
# TYPE proxyhub_backup_last_run_timestamp_seconds gauge
proxyhub_backup_last_run_timestamp_seconds $(date +%s)
# HELP proxyhub_backup_last_run_success 1 if the last backup run succeeded, else 0.
# TYPE proxyhub_backup_last_run_success gauge
proxyhub_backup_last_run_success $ok
# HELP proxyhub_backup_last_success_timestamp_seconds Time of the newest backup file (0 if none).
# TYPE proxyhub_backup_last_success_timestamp_seconds gauge
proxyhub_backup_last_success_timestamp_seconds $last_success
# HELP proxyhub_backup_last_size_bytes Size of the newest encrypted dump.
# TYPE proxyhub_backup_last_size_bytes gauge
proxyhub_backup_last_size_bytes $last_size
# HELP proxyhub_backup_last_duration_seconds Duration of the last run.
# TYPE proxyhub_backup_last_duration_seconds gauge
proxyhub_backup_last_duration_seconds $(($(date +%s) - started))
# HELP proxyhub_backup_files Encrypted backups kept on this server.
# TYPE proxyhub_backup_files gauge
proxyhub_backup_files $count
PROM
  chmod 0644 "$tmp"
  mv -f "$tmp" "$METRICS_DIR/proxyhub_backup.prom"
}

hc_ping() {  # $1: "" on success, "/fail" on failure. Never prints the URL (it is a secret).
  [ -s "$HC_URL_FILE" ] || return 0
  local url
  url="$(tr -d ' \t\r\n' <"$HC_URL_FILE")"
  case "$url" in
    https://*) ;;
    *) log "healthchecks_url is not an https URL; ping skipped"; return 0 ;;
  esac
  # URL via curl's config on stdin, so it is not in the process list either.
  if printf 'url = "%s%s"\n' "$url" "$1" | curl -fsS -m 10 --retry 3 -o /dev/null -K - 2>/dev/null; then
    log "healthchecks ping sent${1:+ ($1)}"
  else
    log "healthchecks ping failed (ignored)"
  fi
}

finish() {
  local rc=$?
  [ -n "$tmp_dump" ] && rm -f "$tmp_dump"
  [ -n "$tmp_manifest" ] && rm -f "$tmp_manifest"
  if [ -n "$pg" ] && [ -n "$manifest_in_container" ]; then
    docker exec "$pg" rm -f "$manifest_in_container" >/dev/null 2>&1 || true
  fi
  write_metrics || log "could not write metrics"
  if [ "$ok" = 1 ]; then hc_ping ""; else hc_ping "/fail"; log "FAILED (exit $rc)"; fi
}
trap finish EXIT

find_postgres() {
  local ids
  ids="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=postgres" --filter status=running)"
  [ "$(echo "$ids" | grep -c .)" = 1 ] || return 1
  echo "$ids"
}

dump_once() {
  local stamp="$1" status
  pg="$(find_postgres)" || { log "no single running postgres container in project $PROJECT"; return 1; }
  manifest_in_container="/tmp/proxyhub-backup-$stamp.manifest"
  tmp_dump="$BACKUP_DIR/.proxyhub-$stamp.dump.age.partial"
  tmp_manifest="$BACKUP_DIR/.proxyhub-$stamp.manifest.age.partial"
  (umask 077
    docker exec -u postgres -e DB_USER="$DB_USER" -e DB_NAME="$DB_NAME" \
      -e MANIFEST="$manifest_in_container" "$pg" \
      bash -c "$(cat "$HERE/dump_in_container.sh")" dump "$(cat "$HERE/row_counts.sql")" |
      age -R "$RECIPIENT_FILE" -o "$tmp_dump"
  ) || { status=$?; log "dump failed (exit $status)"; return 1; }
  local manifest
  manifest="$(docker exec "$pg" cat "$manifest_in_container")" || { log "manifest missing"; return 1; }
  docker exec "$pg" rm -f "$manifest_in_container" >/dev/null || true
  manifest_in_container=""
  if ! grep -q $'^public\\.alembic_version\t' <<<"$manifest"; then
    log "manifest has no public.alembic_version: not a migrated panel database"
    return 1
  fi
  tables="$(grep -c . <<<"$manifest")"
  rows="$(awk -F'\t' '{s += $2} END {print s + 0}' <<<"$manifest")"
  (umask 077; age -R "$RECIPIENT_FILE" -o "$tmp_manifest" <<<"$manifest") || { log "manifest encryption failed"; return 1; }
  size="$(stat -c %s "$tmp_dump")"
  # An age file plus a non-trivial custom-format dump; anything tiny is an empty or broken dump.
  [ "$size" -gt 1024 ] || { log "encrypted dump is only $size bytes"; return 1; }
  head -c 21 "$tmp_dump" | grep -q '^age-encryption.org/v1' || { log "output is not an age file"; return 1; }
}

[ -s "$RECIPIENT_FILE" ] || { log "missing $RECIPIENT_FILE (run deploy/ensure_secrets.sh)"; exit 1; }

stamp=""
for attempt in $(seq 1 "$ATTEMPTS"); do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if dump_once "$stamp"; then break; fi
  rm -f "$tmp_dump" "$tmp_manifest"
  if [ "$attempt" -eq "$ATTEMPTS" ]; then exit 1; fi
  log "attempt $attempt failed; retrying in ${RETRY_PAUSE_S}s"
  sleep "$RETRY_PAUSE_S"
done

final="$BACKUP_DIR/proxyhub-$stamp.dump.age"
sync "$tmp_dump" "$tmp_manifest"
mv -f "$tmp_manifest" "$BACKUP_DIR/proxyhub-$stamp.manifest.age"
mv -f "$tmp_dump" "$final"
tmp_dump=""
tmp_manifest=""
sync "$BACKUP_DIR"

# Retention: keep the newest $KEEP backups (dump + manifest pairs), delete older ones.
mapfile -t all < <(find "$BACKUP_DIR" -maxdepth 1 -name 'proxyhub-*.dump.age' -printf '%f\n' | sort)
if [ "${#all[@]}" -gt "$KEEP" ]; then
  for old in "${all[@]:0:${#all[@]}-KEEP}"; do
    rm -f "$BACKUP_DIR/$old" "$BACKUP_DIR/${old%.dump.age}.manifest.age"
    log "retention: removed $old"
  done
fi

ok=1
log "ok: $(basename "$final") ($size bytes encrypted; $tables tables, $rows rows; keeping $KEEP newest)"
