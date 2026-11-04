#!/usr/bin/env bash
# Restore the database from a backup made by backup.sh.
#
#   ./scripts/restore.sh backups/database-20261104-030000.db.gz
#
# Steps: safety backup of the current DB -> stop bot -> verify + replace DB ->
# start bot -> wait for health. Asks for confirmation (pass --yes to skip).
set -euo pipefail

file="${1:-}"
if [ -z "$file" ] || [ ! -f "$file" ]; then
  echo "Usage: $0 <backup.db.gz> [--yes]" >&2
  exit 1
fi
# Make the path absolute before _common.sh changes into the project root.
file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"
source "$(dirname "$0")/_common.sh"

if [ "${2:-}" != "--yes" ]; then
  read -r -p "Replace the live database with $(basename "$file")? [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || die "Aborted"
fi

log "Taking a safety backup of the current database first"
./scripts/backup.sh || log "Safety backup failed (maybe no database yet), continuing"

log "Stopping the bot"
docker compose stop bot

# Runs in a one-off container as the app user, so file ownership stays correct.
read -r -d '' PY <<'PYEOF' || true
import os, shutil, sqlite3, sys
tmp = "/app/data/restore.tmp"
with open(tmp, "wb") as f:
    shutil.copyfileobj(sys.stdin.buffer, f)
conn = sqlite3.connect(tmp)
try:
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    rows = conn.execute("SELECT count(*) FROM processed_news").fetchone()[0]
finally:
    conn.close()
if not ok:
    os.remove(tmp)
    sys.exit("Backup file failed integrity check; live database untouched")
for suffix in ("-wal", "-shm"):
    try:
        os.remove("/app/data/database.db" + suffix)
    except FileNotFoundError:
        pass
os.replace(tmp, "/app/data/database.db")
print(f"Restored {rows} rows")
PYEOF

if ! gunzip -c "$file" | docker compose run --rm --no-deps -T bot python -c "$PY"; then
  log "Restore failed; starting the bot on the unchanged database"
  docker compose up -d bot
  die "Restore failed"
fi

log "Starting the bot"
docker compose up -d bot
wait_healthy 120 || die "Bot did not become healthy after restore; check: docker compose logs bot"
log "Restore complete"
