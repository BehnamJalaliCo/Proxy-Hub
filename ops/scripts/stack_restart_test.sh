#!/usr/bin/env bash
# Real restart test of the core stack: up, healthy, restart everything, restart redis alone,
# healthy again each time. Needs /etc/proxy-hub/secrets/{db_password,redis_password}.
# Usage: sudo bash ops/scripts/stack_restart_test.sh [compose-project]   (default: proxyhub-rt)
set -euo pipefail

PROJECT="${1:-proxyhub-rt}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE=(docker compose -p "$PROJECT" -f "$ROOT/deploy/docker-compose.yml")
URL=http://127.0.0.1:8000/healthz

wait_ok() {
  local label="$1" end=$((SECONDS + 120))
  while [ $SECONDS -lt $end ]; do
    if curl -fsS --max-time 5 "$URL" >/dev/null 2>&1; then echo "PASS $label"; return 0; fi
    sleep 3
  done
  echo "FAIL $label: $(curl -sS --max-time 5 "$URL" 2>&1 || true)"
  "${COMPOSE[@]}" ps
  return 1
}

"${COMPOSE[@]}" up -d --build
wait_ok "first start"
"${COMPOSE[@]}" restart
wait_ok "restart all services"
"${COMPOSE[@]}" restart redis
wait_ok "restart redis alone"
"${COMPOSE[@]}" restart redis
wait_ok "restart redis again"
echo "ALL PASS"
