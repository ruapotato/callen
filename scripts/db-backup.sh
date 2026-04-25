#!/bin/bash
# Callen database backup script
# Creates a timestamped copy of callen.db + WAL sidecars in backups/
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."

DB="callen.db"
BACKUP_DIR="backups"
TS=$(date +%Y%m%d-%H%M%S)

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB" ]; then
    echo "ERROR: $DB not found"
    exit 1
fi

# Use sqlite3 .backup for a consistent snapshot (handles WAL safely)
if command -v sqlite3 &>/dev/null; then
    sqlite3 "$DB" ".backup '$BACKUP_DIR/${DB}.bak-${TS}'"
else
    # Fallback: raw copy (less safe if writes are in-flight)
    cp "$DB" "$BACKUP_DIR/${DB}.bak-${TS}"
    [ -f "${DB}-shm" ] && cp "${DB}-shm" "$BACKUP_DIR/${DB}-shm.bak-${TS}"
    [ -f "${DB}-wal" ] && cp "${DB}-wal" "$BACKUP_DIR/${DB}-wal.bak-${TS}"
fi

SIZE=$(du -sh "$BACKUP_DIR/${DB}.bak-${TS}" | cut -f1)
echo "Backup complete: $BACKUP_DIR/${DB}.bak-${TS} ($SIZE)"

# Prune backups older than 30 days
find "$BACKUP_DIR" -name "callen.db.bak-*" -mtime +30 -delete 2>/dev/null || true
REMAINING=$(ls -1 "$BACKUP_DIR"/callen.db.bak-* 2>/dev/null | wc -l)
echo "Backups on disk: $REMAINING (pruned >30 days)"
