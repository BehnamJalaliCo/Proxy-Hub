#!/usr/bin/env bash
# Validate every monitoring config with the SAME pinned images the stack runs (no network needed
# besides pulling them): Prometheus config + rules + rule unit tests, Alertmanager config in all
# four rendered variants (Telegram on/off x watchdog on/off), Loki config, Alloy config.
# Usage: bash deploy/monitoring/check_configs.sh
# Image overrides (e.g. a registry mirror of the same digest): PROXYHUB_PROMETHEUS_IMAGE,
# PROXYHUB_ALERTMANAGER_IMAGE, PROXYHUB_LOKI_IMAGE, PROXYHUB_ALLOY_IMAGE.
set -euo pipefail

MON="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$MON/../docker-compose.yml"

# Default image = the pinned reference in docker-compose.yml (single source of truth).
pinned() {
  local var="$1" ref
  ref="$(grep -o "\${$var:-[^}]*}" "$COMPOSE_FILE" | head -1)"
  ref="${ref#*:-}"
  echo "${ref%\}}"
}
PROM="${PROXYHUB_PROMETHEUS_IMAGE:-$(pinned PROXYHUB_PROMETHEUS_IMAGE)}"
AM="${PROXYHUB_ALERTMANAGER_IMAGE:-$(pinned PROXYHUB_ALERTMANAGER_IMAGE)}"
LOKI="${PROXYHUB_LOKI_IMAGE:-$(pinned PROXYHUB_LOKI_IMAGE)}"
ALLOY="${PROXYHUB_ALLOY_IMAGE:-$(pinned PROXYHUB_ALLOY_IMAGE)}"

run() { docker run --rm --network none --user 65534:65534 "$@"; }
fail=0
check() {
  local label="$1"
  shift
  if out="$("$@" 2>&1)"; then
    echo "PASS $label"
  else
    echo "FAIL $label"
    echo "$out" | tail -40 | sed 's/^/  | /'
    fail=1
  fi
}

check "prometheus config" run -v "$MON/prometheus:/etc/prometheus:ro" --entrypoint /bin/promtool "$PROM" \
  check config /etc/prometheus/prometheus.yml
check "prometheus rules" run -v "$MON/prometheus:/etc/prometheus:ro" --entrypoint /bin/promtool "$PROM" \
  check rules /etc/prometheus/rules/panel.yml
check "prometheus rule unit tests" run -v "$MON/prometheus:/etc/prometheus:ro" -w /etc/prometheus/tests \
  --entrypoint /bin/promtool "$PROM" test rules panel_test.yml

# Alertmanager: render each variant with throwaway, obviously fake inputs, then amtool check-config.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 0755 "$work"
# variant | expected receivers line. "malformed" must fall back to null receivers, never half-on.
for spec in "none|off;off" "telegram|on;off" "watchdog|off;on" "both|on;on" "malformed|off;off"; do
  variant="${spec%%|*}"
  want="${spec#*|}"
  want="telegram receiver: ${want%%;*}; external watchdog: ${want#*;}"
  sec="$work/$variant"
  mkdir -p "$sec"
  : > "$sec/alert_bot_token"
  : > "$sec/alert_chat_id"
  : > "$sec/healthchecks_watchdog_url"
  if [ "$variant" = telegram ] || [ "$variant" = both ]; then
    echo "123456:not-a-real-token-config-check-only" > "$sec/alert_bot_token"
    echo "-1001234567890" > "$sec/alert_chat_id"
  fi
  if [ "$variant" = watchdog ] || [ "$variant" = both ]; then
    echo "https://hc-ping.com/00000000-0000-0000-0000-000000000000" > "$sec/healthchecks_watchdog_url"
  fi
  if [ "$variant" = malformed ]; then
    echo "not a token" > "$sec/alert_bot_token"
    echo "@not_numeric" > "$sec/alert_chat_id"
    echo "http://plain-http.example/ping" > "$sec/healthchecks_watchdog_url"
  fi
  chmod -R a+rX "$sec"
  if out="$(run \
    -v "$MON/alertmanager:/etc/alertmanager/proxyhub:ro" \
    -v "$sec/alert_bot_token:/run/secrets/alert_bot_token:ro" \
    -v "$sec/alert_chat_id:/run/secrets/alert_chat_id:ro" \
    -v "$sec/healthchecks_watchdog_url:/run/secrets/healthchecks_watchdog_url:ro" \
    --tmpfs /tmp:mode=1777 \
    -e PROXYHUB_RENDER_ONLY=1 --entrypoint /bin/sh "$AM" /etc/alertmanager/proxyhub/entrypoint.sh 2>&1)" &&
    grep -qF "$want" <<<"$out" && grep -q SUCCESS <<<"$out"; then
    echo "PASS alertmanager config ($variant: $want)"
  else
    echo "FAIL alertmanager config ($variant, expected: $want)"
    echo "$out" | tail -20 | sed 's/^/  | /'
    fail=1
  fi
done

check "loki config" run -v "$MON/loki:/etc/loki/proxyhub:ro" --entrypoint /usr/bin/loki "$LOKI" \
  -config.file=/etc/loki/proxyhub/loki.yml -verify-config
check "alloy config" run -v "$MON/alloy:/etc/alloy/proxyhub:ro" --entrypoint /bin/alloy "$ALLOY" \
  validate /etc/alloy/proxyhub/config.alloy

if [ "$fail" -ne 0 ]; then
  echo "SOME CHECKS FAILED"
  exit 1
fi
echo "ALL PASS"
