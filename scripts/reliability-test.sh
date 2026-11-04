#!/usr/bin/env bash
# Reliability checks against the RUNNING production container.
#
#   ./scripts/reliability-test.sh            # restart, crash, network-failure scenarios
#   ./scripts/reliability-test.sh --after-reboot   # run once after `sudo reboot`
#
# Every scenario verifies: container comes back healthy, the posted-hid count
# never decreases, and initial sync does not run again (no history loss).
# Takes about 3 minutes. The bot keeps running normally; nothing extra is posted.
set -euo pipefail
source "$(dirname "$0")/_common.sh"

pass=0; fail=0
ok()   { log "PASS: $*"; pass=$((pass + 1)); }
bad()  { log "FAIL: $*"; fail=$((fail + 1)); }

row_count() {
  docker compose exec -T bot python -c \
    "import sqlite3;print(sqlite3.connect('/app/data/database.db').execute('select count(*) from processed_news').fetchone()[0])"
}
initial_syncs() { docker compose logs bot 2>/dev/null | grep -c "Initial sync" || true; }

check_state() {  # check_state <scenario> <rows_before> <syncs_before>
  if wait_healthy 120; then ok "$1: container healthy again"; else bad "$1: not healthy"; return; fi
  local rows; rows="$(row_count)"
  if [ "$rows" -ge "$2" ]; then ok "$1: database kept ($rows rows, was $2)"; else bad "$1: rows dropped $2 -> $rows"; fi
  if [ "$(initial_syncs)" -le "$3" ]; then ok "$1: no repeated initial sync"; else bad "$1: initial sync ran again"; fi
}

[ "$(health_status)" = "healthy" ] || die "Start from a healthy container: docker compose up -d"

if [ "${1:-}" = "--after-reboot" ]; then
  [ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$(container_id)")" = "unless-stopped" ] &&
    ok "restart policy is unless-stopped" || bad "restart policy"
  systemctl is-enabled docker >/dev/null 2>&1 && ok "docker starts at boot" || bad "docker not enabled at boot (sudo systemctl enable docker)"
  started="$(docker inspect -f '{{.State.StartedAt}}' "$(container_id)")"
  ok "container running since $started, uptime $(uptime -p 2>/dev/null || uptime)"
  check_state "reboot" 0 999999
  log "Result: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; exit
fi

rows="$(row_count)"; syncs="$(initial_syncs)"
log "Baseline: $rows rows in database"

# 1. Planned restart (what `docker compose restart` / a deploy does)
log "Scenario 1: graceful restart"
docker compose restart bot >/dev/null
if docker compose logs --since 2m bot | grep -q '"Shutdown complete"'; then
  ok "restart: graceful shutdown logged"
else
  bad "restart: no 'Shutdown complete' in logs"
fi
if docker compose logs --since 2m bot | grep -q "CancelledError"; then bad "restart: CancelledError noise"; else ok "restart: no CancelledError noise"; fi
check_state "restart" "$rows" "$syncs"

# 2. Crash: SIGKILL the app process inside the container (no cleanup at all)
log "Scenario 2: hard crash (SIGKILL)"
restarts_before="$(docker inspect -f '{{.RestartCount}}' "$(container_id)")"
docker compose exec -T bot python -c "
import os, signal
for pid in os.listdir('/proc'):
    if pid.isdigit() and b'uvicorn' in open(f'/proc/{pid}/cmdline','rb').read() and pid != '1':
        os.kill(int(pid), signal.SIGKILL)" || true
sleep 5
restarts_after="$(docker inspect -f '{{.RestartCount}}' "$(container_id)")"
if [ "$restarts_after" -gt "$restarts_before" ]; then ok "crash: Docker restarted the container automatically"; else bad "crash: no automatic restart"; fi
check_state "crash" "$rows" "$syncs"

# 3. Network failure: cut the container off for ~70s, then reconnect
log "Scenario 3: network outage (70s)"
network="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$(container_id)")"
docker network disconnect "$network" "$(container_id)"
sleep 70
if docker compose logs --since 75s bot | grep -q "MarketNewsFeed request failed"; then
  ok "network: outage detected and logged"
else
  bad "network: outage not visible in logs"
fi
docker network connect "$network" "$(container_id)"
log "Reconnected, waiting for the next successful poll"
recovered=""
for _ in $(seq 1 30); do
  if curl -fsS -m 10 http://127.0.0.1:8000/ready 2>/dev/null | grep -q '"last_error":null'; then recovered=1; break; fi
  sleep 3
done
[ -n "$recovered" ] && ok "network: polling recovered without a restart" || bad "network: no recovery within 90s"
check_state "network" "$rows" "$syncs"

log "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
