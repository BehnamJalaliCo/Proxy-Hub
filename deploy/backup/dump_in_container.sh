#!/usr/bin/env bash
# Runs INSIDE the panel's postgres container (bash from the postgres image), started by backup.sh:
#   docker exec -u postgres -e DB_USER -e DB_NAME -e MANIFEST <pg> bash -c "<this file>" dump "<sql>"
# Writes a custom-format, compressed pg_dump to STDOUT and the row-count manifest to $MANIFEST, both
# from ONE snapshot: a psql session opens a REPEATABLE READ transaction, exports its snapshot and
# writes the manifest, then pg_dump --snapshot dumps exactly that state while the session waits.
# $1 = the row-count SQL (deploy/backup/row_counts.sql). Only stdout carries data; logs go to stderr.
set -euo pipefail

counts_sql="$1"
: "${DB_USER:?}" "${DB_NAME:?}" "${MANIFEST:?}"

work="$(mktemp -d)"
psql_pid=""
cleanup() {
  exec 3>&- 2>/dev/null || true
  if [ -n "$psql_pid" ]; then kill "$psql_pid" 2>/dev/null || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

mkfifo "$work/ctl"
psql -X -q -At -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \
  <"$work/ctl" >"$work/psql.out" 2>"$work/psql.err" &
psql_pid=$!
exec 3>"$work/ctl"

{
  echo "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
  echo "\\o $work/snapshot"
  echo "SELECT pg_export_snapshot();"
  echo "\\o $MANIFEST"
  printf '%s\n' "$counts_sql"
  echo "\\o"
  echo "\\! touch $work/ready"
} >&3

for _ in $(seq 1 600); do
  [ -e "$work/ready" ] && break
  if ! kill -0 "$psql_pid" 2>/dev/null; then
    echo "dump_in_container: snapshot session failed:" >&2
    tail -5 "$work/psql.err" >&2
    exit 1
  fi
  sleep 0.1
done
[ -e "$work/ready" ] || { echo "dump_in_container: snapshot session timed out" >&2; exit 1; }
snapshot="$(tr -d '[:space:]' <"$work/snapshot")"
[ -n "$snapshot" ] || { echo "dump_in_container: no snapshot id" >&2; exit 1; }

pg_dump -U "$DB_USER" -d "$DB_NAME" --snapshot="$snapshot" --format=custom --compress=6

echo "COMMIT;" >&3
exec 3>&-
wait "$psql_pid"
psql_pid=""
