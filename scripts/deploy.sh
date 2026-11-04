#!/usr/bin/env bash
# Deploy the latest code: pull, build, start, wait for health, roll back on failure.
#
#   ./scripts/deploy.sh            # git pull + build + restart
#   ./scripts/deploy.sh --no-pull  # build what is already on disk
#
# Used by hand on the VPS and by GitHub Actions over SSH.
set -euo pipefail
source "$(dirname "$0")/_common.sh"

[ -f .env ] || die ".env is missing. Copy .env.example to .env and fill it in."

if [ "${1:-}" != "--no-pull" ] && [ -d .git ]; then
  log "Pulling latest code"
  git pull --ff-only
fi
log "Deploying commit $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Keep the currently running image as :previous so a bad release can be undone
# in seconds without rebuilding.
if docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
  docker tag "$IMAGE:latest" "$IMAGE:previous"
fi

docker compose up -d --build --remove-orphans

if wait_healthy 120; then
  log "Deploy OK"
  docker image prune -f >/dev/null  # drop dangling layers from old builds
  exit 0
fi

log "New release is not healthy. Last logs:"
docker compose logs --tail 40 bot || true

if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
  log "Rolling back to the previous image"
  docker tag "$IMAGE:previous" "$IMAGE:latest"
  docker compose up -d --no-build
  if wait_healthy 120; then
    send_alert "Deploy failed and was rolled back. The server's code checkout is newer than the running image; fix and redeploy."
    die "Deploy failed; rolled back to the previous image"
  fi
fi
send_alert "Deploy failed and the bot is NOT healthy. Check: docker compose logs bot"
die "Deploy failed and rollback was not possible"
