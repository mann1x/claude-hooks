#!/bin/bash
# pgvector_backup.sh — daily backup with daily/weekly/monthly retention.
#
# Runs `pg_dump -Fc` (custom binary format, internally compressed) inside
# the mcp-pgvector container and writes to:
#   <BACKUP_DIR>/daily/pgvector-YYYY-MM-DD.dump        (kept 7)
#   <BACKUP_DIR>/weekly/pgvector-YYYY-Www.dump         (kept 4, on Sunday)
#   <BACKUP_DIR>/monthly/pgvector-YYYY-MM.dump         (kept 3, on day 1)
#
# Promotion is by hardlink so weekly/monthly don't double the storage cost.
#
# Tunables (env or override in the systemd unit):
#   CONTAINER         mcp-pgvector
#   PG_USER           claude
#   PG_DB             memory
#   BACKUP_DIR        /shared/config/mcp-pgvector/backups
#   KEEP_DAILY        7
#   KEEP_WEEKLY       4
#   KEEP_MONTHLY      3
#   WEEKLY_DOW        7    (1=Mon … 7=Sun, ISO weekday for `date +%u`)
#
# pg_dump takes only ACCESS SHARE locks, so all reads + writes proceed
# unblocked. The dump is point-in-time-consistent at start.
set -euo pipefail

CONTAINER=${CONTAINER:-mcp-pgvector}
PG_USER=${PG_USER:-claude}
PG_DB=${PG_DB:-memory}
BACKUP_DIR=${BACKUP_DIR:-/shared/config/mcp-pgvector/backups}
KEEP_DAILY=${KEEP_DAILY:-7}
KEEP_WEEKLY=${KEEP_WEEKLY:-4}
KEEP_MONTHLY=${KEEP_MONTHLY:-3}
WEEKLY_DOW=${WEEKLY_DOW:-7}

log() { printf '[pgvector-backup] %s\n' "$*"; }
die() { printf '[pgvector-backup] ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found in PATH"

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q '^true$'; then
    die "container '$CONTAINER' is not running — refusing to back up"
fi

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly" "$BACKUP_DIR/monthly"

today=$(date +%Y-%m-%d)
iso_year=$(date +%G)
iso_week=$(date +%V)
month=$(date +%Y-%m)
dow=$(date +%u)
dom=$(date +%d)

daily_file="$BACKUP_DIR/daily/pgvector-$today.dump"
weekly_file="$BACKUP_DIR/weekly/pgvector-$iso_year-W$iso_week.dump"
monthly_file="$BACKUP_DIR/monthly/pgvector-$month.dump"

start=$(date +%s)
log "starting pg_dump → $daily_file"

# -Fc = custom binary format (compressed); pg_restore uses it to do
# selective / parallel restore. Stream straight to a tempfile and
# atomic-rename so a partial dump never replaces a good one.
tmp=$(mktemp -p "$BACKUP_DIR/daily" ".pgvector-$today.XXXXXX.dump.partial")
trap 'rm -f "$tmp"' EXIT

# --no-owner / --no-privileges keep the dump portable across re-creates
# of the container (the role inside the dump matches whatever the new
# container's POSTGRES_USER is).
if ! docker exec -i "$CONTAINER" pg_dump \
        -U "$PG_USER" -d "$PG_DB" \
        -Fc --no-owner --no-privileges \
        > "$tmp"; then
    die "pg_dump failed"
fi

# Sanity: empty dump = something went wrong.
if [ ! -s "$tmp" ]; then
    die "pg_dump produced an empty file"
fi

mv -f "$tmp" "$daily_file"
trap - EXIT

size=$(du -h "$daily_file" | cut -f1)
elapsed=$(( $(date +%s) - start ))
log "wrote $daily_file ($size) in ${elapsed}s"

# Promote to weekly on configured DOW (Sunday by default).
if [ "$dow" = "$WEEKLY_DOW" ]; then
    if [ ! -e "$weekly_file" ]; then
        ln -f "$daily_file" "$weekly_file"
        log "promoted → $weekly_file"
    fi
fi

# Promote to monthly on day 1.
if [ "$dom" = "01" ]; then
    if [ ! -e "$monthly_file" ]; then
        ln -f "$daily_file" "$monthly_file"
        log "promoted → $monthly_file"
    fi
fi

# Rotation: keep N most-recent files in each tier, by mtime. Uses
# find -printf so empty directories don't trip pipefail (ls exits 2
# on empty glob).
rotate() {
    local dir="$1" keep="$2"
    [ -d "$dir" ] || return 0
    local extras
    extras=$(find "$dir" -maxdepth 1 -name 'pgvector-*.dump' -type f -printf '%T@ %p\n' \
             | sort -nr | tail -n +"$((keep + 1))" | cut -d' ' -f2-)
    [ -z "$extras" ] && return 0
    while IFS= read -r f; do
        rm -f -- "$f"
        log "pruned $(basename "$f")"
    done <<< "$extras"
}
rotate "$BACKUP_DIR/daily"   "$KEEP_DAILY"
rotate "$BACKUP_DIR/weekly"  "$KEEP_WEEKLY"
rotate "$BACKUP_DIR/monthly" "$KEEP_MONTHLY"

count() { find "$1" -maxdepth 1 -name 'pgvector-*.dump' -type f 2>/dev/null | wc -l; }
log "done. tiers: $(count "$BACKUP_DIR/daily") daily, $(count "$BACKUP_DIR/weekly") weekly, $(count "$BACKUP_DIR/monthly") monthly"
