#!/usr/bin/env bash
# Lightweight watchdog, meant to run from cron every 2 minutes.
#
# - Restarts the container if Docker marks it "unhealthy" (plain Docker
#   Compose only reports that status, it never acts on it).
# - Sends a Telegram alert when something breaks, and once when it recovers.
#   Repeats the alert at most hourly while the problem lasts.
# - Pings HEARTBEAT_URL (healthchecks.io / Better Stack) when all is well, so
#   you are also alerted if the whole VPS or cron dies.
#
# Settings in .env: ALERT_CHAT_ID (required for alerts), ALERT_BOT_TOKEN
# (optional, defaults to BOT_TOKEN), HEARTBEAT_URL (optional),
# MONITOR_ERROR_THRESHOLD (optional, ERROR log lines per 10 min, default 5).
set -uo pipefail
source "$(dirname "$0")/_common.sh"

STATE_FILE=".monitor_state"
HEARTBEAT_URL="$(env_get HEARTBEAT_URL)"
ERROR_THRESHOLD="$(env_get MONITOR_ERROR_THRESHOLD)"; ERROR_THRESHOLD="${ERROR_THRESHOLD:-5}"

problems=()

status="$(health_status)"
case "$status" in
  healthy | starting) ;;
  unhealthy)
    docker compose restart bot >/dev/null 2>&1
    problems+=("container was unhealthy and has been restarted")
    ;;
  missing | exited | dead | created)
    # Not restarted automatically: it may have been stopped on purpose.
    problems+=("container is not running (state: $status)")
    ;;
  *) problems+=("container state: $status") ;;
esac

if [ "$status" = "healthy" ]; then
  ready="$(curl -sS -m 15 http://127.0.0.1:8000/ready 2>&1)" || true
  case "$ready" in
    *'"status":"ready"'*) ;;
    *) problems+=("/ready failing: ${ready:0:300}") ;;
  esac

  errors="$(docker compose logs --since 10m bot 2>/dev/null | grep -c '"level": "ERROR"' || true)"
  if [ "${errors:-0}" -ge "$ERROR_THRESHOLD" ]; then
    problems+=("$errors ERROR log lines in the last 10 minutes")
  fi
fi

previous="$(cat "$STATE_FILE" 2>/dev/null || echo "ok 0")"
prev_state="${previous%% *}"
prev_alert="${previous##* }"
now="$(date +%s)"

if [ ${#problems[@]} -eq 0 ]; then
  [ -n "$HEARTBEAT_URL" ] && curl -fsS -m 10 -o /dev/null --retry 2 "$HEARTBEAT_URL"
  if [ "$prev_state" = "fail" ]; then
    send_alert "RECOVERED: bot is healthy again"
  fi
  echo "ok 0" >"$STATE_FILE"
  exit 0
fi

message="PROBLEM: $(IFS=";"; echo "${problems[*]}")"
log "$message"
if [ "$prev_state" != "fail" ] || [ $((now - prev_alert)) -ge 3600 ]; then
  send_alert "$message"
  prev_alert="$now"
fi
echo "fail $prev_alert" >"$STATE_FILE"
exit 1
