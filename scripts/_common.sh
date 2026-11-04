# Shared helpers for the ops scripts. Sourced, not executed.
# All scripts run from the project root, whatever directory you call them from.

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

IMAGE="marketnewsfeed-telegram-bot"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# Read one value from .env WITHOUT sourcing it (sourcing would execute it).
env_get() {
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

container_id() { docker compose ps -aq bot 2>/dev/null; }

health_status() {
  local cid
  cid="$(container_id)"
  [ -n "$cid" ] || { echo "missing"; return; }
  # A stopped container keeps its last health status, so check "running" first.
  docker inspect -f '{{if ne .State.Status "running"}}{{.State.Status}}{{else if .State.Health}}{{.State.Health.Status}}{{else}}running{{end}}' "$cid"
}

# wait_healthy <seconds>: returns 0 once the container reports healthy.
wait_healthy() {
  local deadline=$((SECONDS + $1)) status
  while [ $SECONDS -lt $deadline ]; do
    status="$(health_status)"
    [ "$status" = "healthy" ] && return 0
    sleep 3
  done
  log "Container still '$(health_status)' after $1s"
  return 1
}

# Telegram alert. The token goes through curl's stdin config, so it never
# appears in `ps` output or shell history.
send_alert() {
  local token chat
  token="$(env_get ALERT_BOT_TOKEN)"; token="${token:-$(env_get BOT_TOKEN)}"
  chat="$(env_get ALERT_CHAT_ID)"
  [ -n "$token" ] && [ -n "$chat" ] || { log "ALERT (not sent, ALERT_CHAT_ID unset): $1"; return 0; }
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" |
    curl -sS -m 15 -o /dev/null -K - \
      --data-urlencode "chat_id=$chat" \
      --data-urlencode "text=[mnewsbot $(hostname)] $1" ||
    log "Failed to send Telegram alert"
}
