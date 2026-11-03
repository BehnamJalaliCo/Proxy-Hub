#!/bin/sh
# Alertmanager entrypoint (busybox sh): renders the config at container start, then runs Alertmanager.
#
# Receiver "owner" always POSTs to the panel API (http://api:8000/internal/alerts/alertmanager, the
# backend network; Caddy never exposes /internal/* publicly). The panel sends the Persian message to
# the owner's private chat with the bot the owner entered in the dashboard (panel/proxyhub/telegram).
# Optional secret files (created EMPTY by deploy/ensure_secrets.sh; empty = off) add more receivers:
#   alert_bot_token + alert_chat_id  -> "owner" also sends a Telegram message DIRECTLY (a fallback
#                                       for when the panel API itself is down). If either is empty or
#                                       malformed, only the panel webhook is used.
#   healthchecks_watchdog_url        -> "watchdog": the always-firing Watchdog alert is POSTed there
#                                       every 5 minutes (external dead man's switch). Empty: null.
# Secret values are never printed. They are copied (trimmed) to /tmp, a tmpfs, and Alertmanager reads
# them through *_file settings, so no secret is written into the rendered config. Alertmanager's own
# log goes through redact.awk (see the end of this file): v0.34.2 logs the bot token on send errors.
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
    webhook_configs:
      - url: http://api:8000/internal/alerts/alertmanager
        send_resolved: true
        timeout: 30s
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
echo "alertmanager-entrypoint: panel webhook: on; telegram receiver: $telegram; external watchdog: $watchdog"

if [ "${PROXYHUB_RENDER_ONLY:-0}" = 1 ]; then
  exec /bin/amtool check-config "$CONFIG"
fi

# Rule 3 at runtime: Alertmanager v0.34.2 logs the full Telegram API URL (".../bot<TOKEN>/...") when
# a send fails. So Alertmanager is NOT exec'd: it runs as a child and everything it prints goes
# through redact.awk (secret values read from the tmpfs copies, never from argv) before reaching the
# container log. If the redactor dies, Alertmanager's next log write fails on the closed pipe and it
# exits (fail closed; the container restarts). This shell stays PID 1 only to forward signals:
# TERM/INT -> TERM (graceful stop), HUP -> HUP (config reload); it exits with Alertmanager's code.
LOG_FIFO="$RUNDIR/log.fifo"
rm -f "$LOG_FIFO"
mkfifo -m 0600 "$LOG_FIFO"
awk -v secret_files="$RUNDIR/secrets/alert_bot_token $RUNDIR/secrets/healthchecks_watchdog_url" \
  -f /etc/alertmanager/proxyhub/redact.awk <"$LOG_FIFO" >&2 &
redactor=$!
/bin/alertmanager \
  --config.file="$CONFIG" \
  --storage.path=/alertmanager \
  --web.listen-address=:9093 \
  --cluster.listen-address= \
  --log.level=info >"$LOG_FIFO" 2>&1 &
am=$!
trap 'kill -TERM "$am" 2>/dev/null || true' TERM INT
trap 'kill -HUP "$am" 2>/dev/null || true' HUP
# A trapped signal interrupts "wait" (status > 128) while Alertmanager still runs: wait again.
rc=0
while :; do
  if wait "$am"; then rc=0; else rc=$?; fi
  kill -0 "$am" 2>/dev/null || break
done
wait "$redactor" 2>/dev/null || true
exit "$rc"
