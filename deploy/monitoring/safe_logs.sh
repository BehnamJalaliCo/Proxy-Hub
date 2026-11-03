#!/usr/bin/env bash
# Print the last lines of one stack service's log with every secret redacted, for reports (rule 3).
#   sudo bash deploy/monitoring/safe_logs.sh <service> [lines] [compose-project]
# Defaults: 30 lines, project "proxyhub". Every value in /etc/proxy-hub/secrets/* (and the part after
# a ":" in it, e.g. the secret half of a bot token) and every Telegram Bot API token in a URL is
# replaced by <redacted> (alertmanager/redact.awk, the same filter Alertmanager's log goes through).
# Secret values are read by awk from the files; they never appear in argv or in the output.
set -euo pipefail

SERVICE="${1:?usage: safe_logs.sh <service> [lines] [compose-project]}"
LINES="${2:-30}"
PROJECT="${3:-proxyhub}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SECRETS="${PROXYHUB_SECRETS_DIR:-/etc/proxy-hub/secrets}"
[[ "$LINES" =~ ^[1-9][0-9]{0,5}$ ]] || { echo "lines must be a positive number" >&2; exit 2; }
[ -r "$SECRETS" ] || { echo "cannot read $SECRETS: run with sudo (the filter needs the secret values)" >&2; exit 1; }

files=""
for f in "$SECRETS"/*; do
  [ -f "$f" ] && [ -s "$f" ] && files="$files $f"
done
docker compose -p "$PROJECT" -f "$ROOT/deploy/docker-compose.yml" logs --no-color --tail "$LINES" "$SERVICE" 2>&1 |
  awk -v secret_files="${files# }" -f "$ROOT/deploy/monitoring/alertmanager/redact.awk"
