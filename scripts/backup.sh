#!/usr/bin/env bash
# Back up the SQLite database (the list of already-posted hids).
#
#   ./scripts/backup.sh
#
# Safe while the bot is running: uses SQLite's online backup API, checks the
# copy's integrity, and stores it gzipped in ./backups (BACKUP_DIR in .env).
# Old backups are deleted after BACKUP_KEEP_DAYS (default 14).
set -euo pipefail
source "$(dirname "$0")/_common.sh"

BACKUP_DIR="$(env_get BACKUP_DIR)"; BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="$(env_get BACKUP_KEEP_DAYS)"; KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
target="$BACKUP_DIR/database-$(date -u +%Y%m%d-%H%M%S).db.gz"

# Runs inside the container (as the app user), writes the snapshot to stdout.
read -r -d '' PY <<'PYEOF' || true
import os, sqlite3, sys
src = sqlite3.connect("/app/data/database.db")
dst = sqlite3.connect("/tmp/backup.db")
src.backup(dst)
src.close()
if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("integrity check failed")
rows = dst.execute("SELECT count(*) FROM processed_news").fetchone()[0]
dst.close()
with open("/tmp/backup.db", "rb") as f:
    sys.stdout.buffer.write(f.read())
os.remove("/tmp/backup.db")
print(rows, file=sys.stderr)
PYEOF

if [ -n "$(container_id)" ] && [ "$(docker inspect -f '{{.State.Running}}' "$(container_id)")" = "true" ]; then
  runner=(docker compose exec -T bot)
else
  runner=(docker compose run --rm --no-deps -T bot)  # bot stopped: one-off container
fi

tmp="$target.partial"
if ! "${runner[@]}" python -c "$PY" 2>"$tmp.err" | gzip -9 >"$tmp"; then
  cat "$tmp.err" >&2; rm -f "$tmp" "$tmp.err"
  send_alert "Database backup FAILED"
  die "Backup failed"
fi
rows="$(tail -n 1 "$tmp.err")"; rm -f "$tmp.err"
mv "$tmp" "$target"
chmod 600 "$target"
log "Backup OK: $target ($rows rows, $(du -h "$target" | cut -f1))"

find "$BACKUP_DIR" -name 'database-*.db.gz' -type f -mtime +"$KEEP_DAYS" -print -delete |
  sed 's/^/Deleted old backup: /'
