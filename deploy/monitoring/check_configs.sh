#!/usr/bin/env bash
# Validate every monitoring config with the SAME pinned images the stack runs (no network needed
# besides pulling them): Prometheus config + rules + rule unit tests, Alertmanager config in all
# rendered variants (Telegram on/off x watchdog on/off, malformed), a REAL runtime check that the
# Telegram bot token never reaches the Alertmanager log, Loki config, Alloy config.
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
trap 'rm -rf "$work"' EXIT  # replaced by cleanup() once the leak check can leave a container
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

# Rule 3 at RUNTIME, not just in the rendered config: Alertmanager v0.34.2 logs the full Telegram API
# URL (".../bot<TOKEN>/sendMessage") whenever a send fails. Start the real entrypoint with a random
# dummy token and no network, add an alert, wait for the real failed send, then the container log
# must hold the redacted URL and never the token or its secret half. Then a graceful SIGTERM stop
# (the entrypoint forwards it to Alertmanager): quick, exit 0, nothing leaked while shutting down.
leak_ctr=""
cleanup() {
  [ -z "$leak_ctr" ] || docker rm -f -v "$leak_ctr" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
leak_check() {
  local sec="$work/leak" token half logs end t0 t1 code n
  mkdir -p "$sec"
  token="$((100000000 + RANDOM * 32768 + RANDOM)):LeakCheck$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  half="${token#*:}"
  printf '%s\n' "$token" > "$sec/alert_bot_token"
  echo "-1001234567890" > "$sec/alert_chat_id"
  : > "$sec/healthchecks_watchdog_url"
  chmod -R a+rX "$sec"
  # Every docker output below goes through scrub(): a failure report must not echo even the dummy.
  scrub() {
    awk -v t="$token" -v h="$half" '{
      while ((i = index($0, t)) > 0) $0 = substr($0, 1, i - 1) "<DUMMY-TOKEN>" substr($0, i + length(t))
      while ((i = index($0, h)) > 0) $0 = substr($0, 1, i - 1) "<DUMMY-HALF>" substr($0, i + length(h))
      print
    }'
  }
  leak_ctr="$(docker run -d --network none --user 65534:65534 --read-only \
    --label proxyhub.purpose=config-check \
    -v "$MON/alertmanager:/etc/alertmanager/proxyhub:ro" \
    -v "$sec/alert_bot_token:/run/secrets/alert_bot_token:ro" \
    -v "$sec/alert_chat_id:/run/secrets/alert_chat_id:ro" \
    -v "$sec/healthchecks_watchdog_url:/run/secrets/healthchecks_watchdog_url:ro" \
    --tmpfs /tmp:mode=1777,size=4m --tmpfs /alertmanager:mode=1777,size=16m \
    --entrypoint /bin/sh "$AM" /etc/alertmanager/proxyhub/entrypoint.sh 2>&1)" || {
    echo "FAIL alertmanager runtime leak check: could not start"
    echo "$leak_ctr" | scrub | sed 's/^/  | /'
    leak_ctr=""
    return 1
  }
  end=$((SECONDS + 60))
  until docker exec "$leak_ctr" wget -q -O /dev/null http://127.0.0.1:9093/-/ready 2>/dev/null; do
    if [ $SECONDS -ge $end ]; then
      echo "FAIL alertmanager runtime leak check: not ready within 60s"
      docker logs "$leak_ctr" 2>&1 | tail -20 | scrub | sed 's/^/  | /'
      return 1
    fi
    sleep 1
  done
  if ! docker exec "$leak_ctr" /bin/amtool --alertmanager.url=http://127.0.0.1:9093 alert add \
    alertname=ProxyHubLeakCheck severity=critical --annotation=summary=leak-check >/dev/null 2>&1; then
    echo "FAIL alertmanager runtime leak check: amtool could not add the test alert"
    return 1
  fi
  # group_wait is 30 s for the owner route; then the send fails at once (no network) and is logged.
  end=$((SECONDS + 120))
  until docker logs "$leak_ctr" 2>&1 | grep -q 'Notify attempt failed.*integration=telegram'; do
    if [ $SECONDS -ge $end ]; then
      echo "FAIL alertmanager runtime leak check: no failed Telegram send was logged within 120s"
      docker logs "$leak_ctr" 2>&1 | tail -20 | scrub | sed 's/^/  | /'
      return 1
    fi
    sleep 2
  done
  t0=$SECONDS
  docker stop -t 30 "$leak_ctr" >/dev/null || true
  t1=$SECONDS
  code="$(docker inspect -f '{{.State.ExitCode}}' "$leak_ctr")"
  logs="$(docker logs "$leak_ctr" 2>&1)"
  if grep -qF -e "$token" -e "$half" <<<"$logs"; then
    n="$(grep -cF -e "$token" -e "$half" <<<"$logs")"
    echo "FAIL alertmanager runtime leak check: the bot token is in the container log ($n lines)"
    grep -F -e "$token" -e "$half" <<<"$logs" | head -5 | scrub | sed 's/^/  | /'
    return 1
  fi
  if ! grep -q 'Notify attempt failed.*bot<redacted>' <<<"$logs"; then
    # The send failed but no URL was redacted: the leak path was not exercised (or Alertmanager no
    # longer logs the URL). Either way this check proves nothing; look at it again.
    echo "FAIL alertmanager runtime leak check: no redacted Telegram URL in the failed-send line"
    grep 'Notify attempt failed' <<<"$logs" | head -3 | scrub | sed 's/^/  | /'
    return 1
  fi
  if [ "$code" != 0 ] || [ $((t1 - t0)) -ge 15 ] || ! grep -q 'msg="Received shutdown signal, exiting gracefully' <<<"$logs"; then
    echo "FAIL alertmanager graceful stop: exit $code after $((t1 - t0))s (SIGTERM not forwarded?)"
    tail -5 <<<"$logs" | scrub | sed 's/^/  | /'
    return 1
  fi
  n="$(grep -c 'bot<redacted>' <<<"$logs")"
  echo "PASS alertmanager runtime leak check: real failed Telegram send, token redacted in $n log lines, 0 leaks"
  echo "PASS alertmanager graceful stop: SIGTERM forwarded, exit 0 after $((t1 - t0))s"
  docker rm -f -v "$leak_ctr" >/dev/null 2>&1 || true
  leak_ctr=""
}
leak_check || fail=1

check "loki config" run -v "$MON/loki:/etc/loki/proxyhub:ro" --entrypoint /usr/bin/loki "$LOKI" \
  -config.file=/etc/loki/proxyhub/loki.yml -verify-config
check "alloy config" run -v "$MON/alloy:/etc/alloy/proxyhub:ro" --entrypoint /bin/alloy "$ALLOY" \
  validate /etc/alloy/proxyhub/config.alloy

if [ "$fail" -ne 0 ]; then
  echo "SOME CHECKS FAILED"
  exit 1
fi
echo "ALL PASS"
