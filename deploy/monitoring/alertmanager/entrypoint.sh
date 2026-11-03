#!/bin/sh
# Alertmanager entrypoint (busybox sh): renders the config at container start, then runs Alertmanager.
#
# Receivers depend on optional secret files (created EMPTY by deploy/ensure_secrets.sh; empty = off):
#   alert_bot_token + alert_chat_id  -> "owner": Telegram message to the owner's private chat.
#                                       If either is empty or malformed: a null receiver (alerts are
#                                       still visible in Alertmanager/Grafana, just not sent).
#   healthchecks_watchdog_url        -> "watchdog": the always-firing Watchdog alert is POSTed there
#                                       every 5 minutes (external dead man's switch). Empty: null.
# Secret values are never printed. They are copied (trimmed) to /tmp, a tmpfs, and Alertmanager reads
# them through *_file settings, so no secret is written into the rendered config.
# PROXYHUB_RENDER_ONLY=1: render + amtool check-config, then exit (deploy/monitoring/check_configs.sh).
set -eu

SECRETS=/run/secrets
TPL=/etc/alertmanager/proxyhub/telegram.tmpl
RUNDIR=/tmp/alertmanager
CONFIG="$RUNDIR/alertmanager.yml"

umask 077
mkdir -p "$RUNDIR/secrets"

# copy_secret <name>: trimmed copy into the tmpfs; succeeds only if the file has content.
copy_secret() {
  [ -s "$SECRETS/$1" ] || return 1
  tr -d ' \t\r\n' < "$SECRETS/$1" > "$RUNDIR/secrets/$1"
  [ -s "$RUNDIR/secrets/$1" ]
}

telegram=off
if copy_secret alert_bot_token && copy_secret alert_chat_id; then
  if ! grep -Eq '^[0-9]{3,20}:[A-Za-z0-9_-]{20,100}$' "$RUNDIR/secrets/alert_bot_token"; then
    echo "alertmanager-entrypoint: alert_bot_token does not look like a bot token; Telegram stays off" >&2
  elif ! grep -Eq '^-?[0-9]{1,20}$' "$RUNDIR/secrets/alert_chat_id"; then
    echo "alertmanager-entrypoint: alert_chat_id is not a numeric chat id; Telegram stays off" >&2
  else
    telegram=on
  fi
fi

watchdog=off
if copy_secret healthchecks_watchdog_url; then
  if grep -Eq '^https://[A-Za-z0-9.-]+(:[0-9]+)?/[^[:space:]]*$' "$RUNDIR/secrets/healthchecks_watchdog_url"; then
    watchdog=on
  else
    echo "alertmanager-entrypoint: healthchecks_watchdog_url is not an https URL; watchdog stays off" >&2
  fi
fi

{
  cat <<'YAML'
# Rendered at container start by /etc/alertmanager/proxyhub/entrypoint.sh. Do not edit.
global:
  resolve_timeout: 5m
templates:
  - /etc/alertmanager/proxyhub/telegram.tmpl
route:
  receiver: owner
  group_by: [alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['alertname="Watchdog"']
      receiver: watchdog
      group_wait: 0s
      group_interval: 1m
      repeat_interval: 5m
receivers:
  - name: owner
YAML
  if [ "$telegram" = on ]; then
    chat_id="$(cat "$RUNDIR/secrets/alert_chat_id")"
    cat <<YAML
    telegram_configs:
      - bot_token_file: $RUNDIR/secrets/alert_bot_token
        chat_id: $chat_id
        parse_mode: HTML
        message: '{{ template "proxyhub.telegram.message" . }}'
        send_resolved: true
YAML
  fi
  echo "  - name: watchdog"
  if [ "$watchdog" = on ]; then
    cat <<YAML
    webhook_configs:
      - url_file: $RUNDIR/secrets/healthchecks_watchdog_url
        send_resolved: false
YAML
  fi
} > "$CONFIG"
chmod 0400 "$CONFIG"
[ -r "$TPL" ] || { echo "alertmanager-entrypoint: missing $TPL" >&2; exit 1; }
# Invariant (rule 3): secrets are referenced by *_file, never written into the rendered config.
for name in alert_bot_token healthchecks_watchdog_url; do
  if [ -s "$RUNDIR/secrets/$name" ] && grep -qF -f "$RUNDIR/secrets/$name" "$CONFIG"; then
    echo "alertmanager-entrypoint: refusing to start: $name leaked into the rendered config" >&2
    exit 1
  fi
done
echo "alertmanager-entrypoint: telegram receiver: $telegram; external watchdog: $watchdog"

if [ "${PROXYHUB_RENDER_ONLY:-0}" = 1 ]; then
  exec /bin/amtool check-config "$CONFIG"
fi
exec /bin/alertmanager \
  --config.file="$CONFIG" \
  --storage.path=/alertmanager \
  --web.listen-address=:9093 \
  --cluster.listen-address= \
  --log.level=info
