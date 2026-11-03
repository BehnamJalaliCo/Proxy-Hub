#!/usr/bin/env bash
# Real checks against a RUNNING stack (M1.7). Never prints a secret.
#   sudo bash deploy/monitoring/verify_stack.sh [compose-project]     (default: proxyhub)
# 1. every Prometheus target is up        5. Grafana /api/health ok, datasources + dashboard provisioned
# 2. all rule groups loaded, none broken   6. Loki ready
# 3. Watchdog firing in Prometheus         7. Loki holds panel logs: only api/worker/migrate, no IP
# 4. ... and delivered to Alertmanager     8. Grafana listens on 127.0.0.1 only
# Needs python3 (JSON parsing) and curl on the host. Extra compose files: COMPOSE_EXTRA_FILES.
set -euo pipefail

PROJECT="${1:-proxyhub}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE=(docker compose -p "$PROJECT" -f "$ROOT/deploy/docker-compose.yml")
for extra in ${COMPOSE_EXTRA_FILES:-}; do COMPOSE+=(-f "$extra"); done
GRAFANA_PW_FILE=/etc/proxy-hub/secrets/grafana_admin_password
WAIT_S="${VERIFY_WAIT_S:-180}"

fail=0
pass() { echo "PASS $*"; }
bad() { echo "FAIL $*"; fail=1; }
# GET from inside the prometheus container (busybox wget), which is on the monitoring network.
iget() { "${COMPOSE[@]}" exec -T prometheus wget -q -O - "$1"; }
py() { python3 -I -c "$1"; }
urlq() { python3 -I -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

# 1. targets (scrapes are every 15 s; give a fresh stack time).
end=$((SECONDS + WAIT_S))
while :; do
  targets="$(iget 'http://127.0.0.1:9090/api/v1/targets?state=active' || echo '{}')"
  summary="$(py 'import json,sys
d=json.load(sys.stdin).get("data",{}).get("activeTargets",[])
down=[t["labels"]["job"]+"="+t["health"] for t in d if t["health"]!="up"]
print(len(d), " ".join(sorted(down)))' <<<"$targets")"
  total="${summary%% *}"
  down="${summary#* }"
  [ "$total" = "$summary" ] && down=""
  if [ "$total" -ge 7 ] && [ -z "${down// /}" ]; then break; fi
  [ $SECONDS -lt $end ] || break
  sleep 5
done
if [ "$total" -ge 7 ] && [ -z "${down// /}" ]; then pass "prometheus: all $total targets up"; else bad "prometheus targets: $total active, not up: $down"; fi

# 2. rules
rules="$(iget 'http://127.0.0.1:9090/api/v1/rules')"
read -r groups alerts broken <<<"$(py 'import json,sys
g=json.load(sys.stdin)["data"]["groups"]
r=[x for grp in g for x in grp["rules"]]
print(len(g), sum(1 for x in r if x["type"]=="alerting"), sum(1 for x in r if x.get("health")=="err"))' <<<"$rules")"
if [ "$groups" -ge 4 ] && [ "$broken" = 0 ]; then pass "prometheus: $groups rule groups, $alerts alert rules, 0 broken"; else bad "rules: groups=$groups alerts=$alerts broken=$broken"; fi

# 3 + 4. Watchdog fires and reaches Alertmanager (the Prometheus -> Alertmanager path works).
end=$((SECONDS + WAIT_S))
while :; do
  in_prom="$(iget 'http://127.0.0.1:9090/api/v1/alerts' | py 'import json,sys
print(any(a["labels"].get("alertname")=="Watchdog" and a["state"]=="firing" for a in json.load(sys.stdin)["data"]["alerts"]))')"
  in_am="$(iget 'http://alertmanager:9093/api/v2/alerts' | py 'import json,sys
print(any(a["labels"].get("alertname")=="Watchdog" for a in json.load(sys.stdin)))')"
  [ "$in_prom" = True ] && [ "$in_am" = True ] && break
  [ $SECONDS -lt $end ] || break
  sleep 5
done
if [ "$in_prom" = True ]; then pass "prometheus: Watchdog firing"; else bad "Watchdog not firing in Prometheus"; fi
if [ "$in_am" = True ]; then pass "alertmanager: received Watchdog from Prometheus"; else bad "Watchdog not in Alertmanager"; fi
receivers="$("${COMPOSE[@]}" logs --no-color alertmanager 2>/dev/null | grep -o 'telegram receiver: [a-z]*; external watchdog: [a-z]*' | tail -1 || true)"
echo "INFO alertmanager ${receivers:-receiver line not found}"

# 5. Grafana (published on 127.0.0.1:3000 only).
if curl -fsS --max-time 5 http://127.0.0.1:3000/api/health | py 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("database")=="ok" else 1)'; then
  pass "grafana: /api/health database ok"
else
  bad "grafana /api/health"
fi
if [ -r "$GRAFANA_PW_FILE" ]; then
  # Credentials go to curl through a config on stdin: never in argv, never printed.
  gget() { printf 'user = "admin:%s"\n' "$(tr -d '\r\n' <"$GRAFANA_PW_FILE")" | curl -fsS --max-time 5 -K - "http://127.0.0.1:3000$1"; }
  ds="$(gget /api/datasources | py 'import json,sys; print(",".join(sorted(d["type"] for d in json.load(sys.stdin))))' || echo none)"
  if [ "$ds" = "alertmanager,loki,prometheus" ]; then pass "grafana: datasources $ds"; else bad "grafana datasources: $ds"; fi
  dash="$(gget '/api/search?query=Proxy-Hub' | py 'import json,sys; print(len(json.load(sys.stdin)))' || echo 0)"
  if [ "$dash" -ge 1 ]; then pass "grafana: dashboard provisioned"; else bad "grafana dashboard missing"; fi
else
  echo "SKIP grafana login checks (no read access to $GRAFANA_PW_FILE; run with sudo)"
fi
bind="$(docker port "$("${COMPOSE[@]}" ps -q grafana)" 3000/tcp || true)"
if [ "$bind" = "127.0.0.1:3000" ]; then pass "grafana: published on 127.0.0.1:3000 only"; else bad "grafana published as: $bind"; fi

# 6. Loki
if [ "$(iget http://loki:3100/ready 2>/dev/null || true)" = ready ]; then pass "loki: ready"; else bad "loki not ready"; fi

# 7. Panel logs in Loki: present, only panel services, no IP address left.
end=$((SECONDS + WAIT_S))
while :; do
  now_ns="$(date +%s)000000000"
  start_ns="$(( $(date +%s) - 3600 ))000000000"
  by_service="$(iget "http://loki:3100/loki/api/v1/query?query=$(urlq 'sum by (service) (count_over_time({job="panel"}[1h]))')&time=$now_ns" |
    py 'import json,sys
r=json.load(sys.stdin)["data"]["result"]
print(" ".join(sorted(x["metric"].get("service","?")+"="+x["value"][1] for x in r)))')"
  [ -n "$by_service" ] && break
  [ $SECONDS -lt $end ] || break
  sleep 5
done
if [ -n "$by_service" ]; then pass "loki: panel log lines in the last hour: $by_service"; else bad "loki: no panel logs shipped"; fi
others="$(tr ' ' '\n' <<<"$by_service" | grep -v -E '^(api|worker|migrate)=' | grep . || true)"
if [ -z "$others" ]; then pass "loki: only api/worker/migrate logs (no other container)"; else bad "loki holds logs of: $others"; fi
ip_lines="$(iget "http://loki:3100/loki/api/v1/query_range?query=$(urlq '{job="panel"} |~ "([0-9]{1,3}[.]){3}[0-9]{1,3}"')&start=$start_ns&end=$now_ns&limit=1000" |
  py 'import json,sys; print(sum(len(s["values"]) for s in json.load(sys.stdin)["data"]["result"]))')"
if [ "$ip_lines" = 0 ]; then pass "loki: 0 shipped lines contain an IPv4 address (rule 7)"; else bad "loki: $ip_lines lines contain an IPv4 address"; fi

if [ "$fail" -ne 0 ]; then echo "SOME CHECKS FAILED"; exit 1; fi
echo "ALL PASS"
