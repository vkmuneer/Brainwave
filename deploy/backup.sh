#!/bin/bash
# Daily backup of the SQLite database. Backups are your responsibility (unlike
# a managed cloud database) - this script keeps the last 30 days of copies.
#
# Run it by hand any time:
#
#   ./deploy/backup.sh
#
# Or nightly at 11pm, via cron (crontab -e):
#
#   0 23 * * * /full/path/to/brainwave_academy/deploy/backup.sh
#
# Override either location if you want backups on an external/mounted disk:
#
#   BACKUP_DIR=/Volumes/USB/brainwave ./deploy/backup.sh

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_FILE="${DB_FILE:-$APP_DIR/instance/brainwave.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
DATE=$(date +%Y-%m-%d_%H%M)
DEST="$BACKUP_DIR/brainwave_$DATE.db"

if [ ! -f "$DB_FILE" ]; then
    echo "No database found at $DB_FILE - nothing to back up." >&2
    exit 1
fi

if ! command -v sqlite3 &> /dev/null; then
    echo "sqlite3 is required but not installed (try: sudo apt install sqlite3)." >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# .backup rather than cp: it takes a consistent snapshot even if the app is
# mid-write, whereas a plain copy can capture a half-finished transaction.
sqlite3 "$DB_FILE" ".backup '$DEST'"

find "$BACKUP_DIR" -name "brainwave_*.db" -mtime +30 -delete

echo "Backed up to $DEST ($(du -h "$DEST" | cut -f1))"
