#!/usr/bin/env bash
# Weekly restore test (M1.7). Run as root by proxyhub-restore-test.timer.
#
# Decrypts the NEWEST backup with the private key (/etc/proxy-hub/secrets/backup_age_key) and
# restores it into a THROWAWAY PostgreSQL container: the same image as the running panel postgres
# (same pinned digest), no network at all, capped memory and CPU. Its data and the decrypted dump
# live on two anonymous Docker volumes on disk (labelled proxyhub.purpose=restore-test), not in RAM:
# a tmpfs would be charged to the host's memory and could OOM a small panel server. The live database
# is on the same disk unencrypted anyway; both volumes go with the container. Then it checks:
#   - pg_restore finished with --exit-on-error in one transaction,
#   - the restored database has public.alembic_version and public.settings,
#   - every table's exact row count equals the manifest taken from the same snapshot as the dump.
# The container and its volumes are always destroyed (leftovers of a crashed run are removed at
# the next start). The result goes to node-exporter's textfile directory
# (alerts RestoreTestFailing / RestoreTestTooOld) and to /var/lib/proxy-hub/restore_test.status.
#
# Overrides: PROXYHUB_COMPOSE_PROJECT (default proxyhub), PROXYHUB_BACKUP_DIR, PROXYHUB_METRICS_DIR,
# PROXYHUB_RESTORE_IMAGE (default: image of the running postgres container), PROXYHUB_BACKUP_KEY,
# PROXYHUB_RESTORE_MEMORY (default 512m; memory + swap cap of the throwaway container).
set -euo pipefail

PROJECT="${PROXYHUB_COMPOSE_PROJECT:-proxyhub}"
BACKUP_DIR="${PROXYHUB_BACKUP_DIR:-/var/lib/proxy-hub/backups}"
METRICS_DIR="${PROXYHUB_METRICS_DIR:-/var/lib/proxy-hub/metrics}"
KEY_FILE="${PROXYHUB_BACKUP_KEY:-/etc/proxy-hub/secrets/backup_age_key}"
STATUS_FILE="$(dirname "$BACKUP_DIR")/restore_test.status"
HERE="$(cd "$(dirname "$0")" && pwd)"
READY_TIMEOUT_S=180
MEMORY="${PROXYHUB_RESTORE_MEMORY:-512m}"
LABEL=proxyhub.purpose=restore-test

log() { echo "proxyhub-restore-test: $*"; }

[ "$(id -u)" -eq 0 ] || { log "run as root"; exit 1; }
command -v age >/dev/null || { log "age is not installed (deploy/install.sh installs it)"; exit 1; }
install -d -m 0755 -o root -g root "$METRICS_DIR"
install -d -m 0700 -o root -g root "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.restore_test.lock"
flock -n 9 || { log "another restore test is running"; exit 1; }

started="$(date +%s)"
ok=0
reason="did not finish"
backup_name=""
backup_age=0
tables=0
rows=0
container=""
prev_success="$(sed -n 's/^last_success_epoch=//p' "$STATUS_FILE" 2>/dev/null || true)"
[[ "$prev_success" =~ ^[0-9]+$ ]] || prev_success=0

write_result() {
  local now last_success tmp
  now="$(date +%s)"
  last_success="$prev_success"
  [ "$ok" = 1 ] && last_success="$now"
  tmp="$(mktemp "$METRICS_DIR/.proxyhub_restore_test.prom.XXXXXX")"
  cat >"$tmp" <<PROM
# HELP proxyhub_restore_test_last_run_timestamp_seconds When restore_test.sh last finished.
# TYPE proxyhub_restore_test_last_run_timestamp_seconds gauge
proxyhub_restore_test_last_run_timestamp_seconds $now
# HELP proxyhub_restore_test_last_run_success 1 if the newest backup restored and matched its manifest.
# TYPE proxyhub_restore_test_last_run_success gauge
proxyhub_restore_test_last_run_success $ok
# HELP proxyhub_restore_test_last_success_timestamp_seconds Last successful restore test (0 if none).
# TYPE proxyhub_restore_test_last_success_timestamp_seconds gauge
proxyhub_restore_test_last_success_timestamp_seconds $last_success
# HELP proxyhub_restore_test_tables_checked Tables compared in the last run.
# TYPE proxyhub_restore_test_tables_checked gauge
proxyhub_restore_test_tables_checked $tables
# HELP proxyhub_restore_test_rows_checked Rows (sum of all tables) compared in the last run.
# TYPE proxyhub_restore_test_rows_checked gauge
proxyhub_restore_test_rows_checked $rows
# HELP proxyhub_restore_test_backup_age_seconds Age of the tested backup at test time.
# TYPE proxyhub_restore_test_backup_age_seconds gauge
proxyhub_restore_test_backup_age_seconds $backup_age
# HELP proxyhub_restore_test_duration_seconds Duration of the last run.
# TYPE proxyhub_restore_test_duration_seconds gauge
proxyhub_restore_test_duration_seconds $((now - started))
PROM
  chmod 0644 "$tmp"
  mv -f "$tmp" "$METRICS_DIR/proxyhub_restore_test.prom"
  tmp="$(mktemp "$STATUS_FILE.XXXXXX")"
  cat >"$tmp" <<STATUS
result=$([ "$ok" = 1 ] && echo ok || echo failed)
checked_at=$(date -u -d "@$now" +%Y-%m-%dT%H:%M:%SZ)
backup=$backup_name
backup_age_seconds=$backup_age
tables=$tables
rows=$rows
reason=$reason
last_success_epoch=$last_success
STATUS
  chmod 0644 "$tmp"
  mv -f "$tmp" "$STATUS_FILE"
}

# Remove every throwaway container and volume (they carry the label). The flock above guarantees no
# other restore test is using them.
remove_throwaways() {
  local -a ids
  mapfile -t ids < <(docker ps -a -q --filter "label=$LABEL")
  [ ${#ids[@]} -eq 0 ] || docker rm -f -v "${ids[@]}" >/dev/null 2>&1 || true
  mapfile -t ids < <(docker volume ls -q --filter "label=$LABEL")
  [ ${#ids[@]} -eq 0 ] || docker volume rm -f "${ids[@]}" >/dev/null 2>&1 || true
  [ -z "$(docker ps -a -q --filter "label=$LABEL")$(docker volume ls -q --filter "label=$LABEL")" ]
}

finish() {
  local rc=$?
  if [ -n "$container" ]; then
    remove_throwaways || log "WARNING: a throwaway container or volume ($LABEL) still exists"
  fi
  write_result || log "could not write result"
  if [ "$ok" = 1 ]; then log "PASS: $reason"; else log "FAIL (exit $rc): $reason"; fi
}
trap finish EXIT

fail() { reason="$1"; exit 1; }

[ -s "$KEY_FILE" ] || fail "missing private key $KEY_FILE"
backup_name="$(find "$BACKUP_DIR" -maxdepth 1 -name 'proxyhub-*.dump.age' -printf '%f\n' | sort | tail -1)"
[ -n "$backup_name" ] || fail "no backup found in $BACKUP_DIR"
dump="$BACKUP_DIR/$backup_name"
manifest_file="${dump%.dump.age}.manifest.age"
[ -s "$manifest_file" ] || fail "manifest missing for $backup_name"
backup_age=$(($(date +%s) - $(stat -c %Y "$dump")))
log "testing $backup_name (age ${backup_age}s)"

expected="$(age -d -i "$KEY_FILE" "$manifest_file")" || fail "cannot decrypt the manifest"
grep -q $'^public\\.alembic_version\t' <<<"$expected" || fail "manifest has no public.alembic_version"
grep -q $'^public\\.settings\t' <<<"$expected" || fail "manifest has no public.settings"

remove_throwaways || fail "cannot remove leftovers of an earlier restore test ($LABEL)"
# Room on Docker's disk for the decrypted dump plus the restored database: 1 GiB + 10 x the dump.
need=$((1024 * 1024 * 1024 + 10 * $(stat -c %s "$dump")))
docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
avail="$(df -B1 --output=avail "$docker_root" | tail -1 | tr -d ' ')" ||
  fail "cannot read the free disk under $docker_root"
if ! [[ "$avail" =~ ^[0-9]+$ ]] || [ "$avail" -lt "$need" ]; then
  fail "not enough free disk under $docker_root for the restore test (need $need bytes)"
fi

image="${PROXYHUB_RESTORE_IMAGE:-}"
if [ -z "$image" ]; then
  pg="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=postgres" --filter status=running | head -1)"
  [ -n "$pg" ] || fail "no running postgres in project $PROJECT to take the image from"
  image="$(docker inspect -f '{{.Image}}' "$pg")"
fi
log "throwaway postgres image: $image"

container="proxyhub-restore-test-$(date -u +%Y%m%dT%H%M%SZ)-$$"
# No network; trust auth is only reachable from inside this container. Memory (+ swap) and CPU are
# capped so the test cannot starve the panel. Data and dump: anonymous, labelled volumes (rm -v).
docker run -d --name "$container" --label "$LABEL" --network none \
  --memory "$MEMORY" --memory-swap "$MEMORY" --cpus 1 --pids-limit 256 \
  --mount "type=volume,dst=/var/lib/postgresql/data,volume-label=$LABEL" \
  --mount "type=volume,dst=/restore,volume-label=$LABEL" \
  -e POSTGRES_USER=proxyhub -e POSTGRES_DB=proxyhub -e POSTGRES_HOST_AUTH_METHOD=trust \
  "$image" >/dev/null || fail "could not start the throwaway postgres"
docker exec "$container" sh -c 'chown postgres:postgres /restore && chmod 0700 /restore' ||
  fail "cannot prepare /restore in the throwaway postgres"

end=$((SECONDS + READY_TIMEOUT_S))
# 127.0.0.1 answers only after the entrypoint's init phase (its temporary server is socket-only).
until docker exec -u postgres "$container" pg_isready -q -h 127.0.0.1 -U proxyhub -d proxyhub; do
  [ $SECONDS -lt $end ] || fail "throwaway postgres not ready within ${READY_TIMEOUT_S}s"
  sleep 2
done

age -d -i "$KEY_FILE" "$dump" | docker exec -i -u postgres "$container" sh -c 'cat > /restore/db.dump' ||
  fail "cannot decrypt $backup_name"
docker exec -u postgres "$container" pg_restore -U proxyhub -d proxyhub --exit-on-error \
  --single-transaction /restore/db.dump || fail "pg_restore failed"

actual="$(docker exec -i -u postgres "$container" psql -X -q -At -v ON_ERROR_STOP=1 \
  -U proxyhub -d proxyhub <"$HERE/row_counts.sql")" || fail "cannot count rows in the restored database"

if [ "$expected" != "$actual" ]; then
  diff <(echo "$expected") <(echo "$actual") | head -20 | sed 's/^/  | /' || true
  fail "restored row counts differ from the manifest"
fi
tables="$(grep -c . <<<"$actual")"
rows="$(awk -F'\t' '{s += $2} END {print s + 0}' <<<"$actual")"
ok=1
reason="$backup_name restored; $tables tables and $rows rows match the manifest"
