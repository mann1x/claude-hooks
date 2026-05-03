#!/bin/bash
# pgvector_restore.sh — restore an mcp-pgvector backup produced by
# pgvector_backup.sh.
#
# Usage:
#   pgvector_restore.sh <dump-file>          # latest_daily by default
#   pgvector_restore.sh latest_daily         # most recent daily
#   pgvector_restore.sh latest_weekly
#   pgvector_restore.sh latest_monthly
#
# Calls pg_restore inside the container. By default uses --clean so it
# drops the existing schema first; pass NO_CLEAN=1 to merge into an
# empty / non-conflicting DB instead.
#
# THIS IS DESTRUCTIVE — it overwrites the live DB. Asks for confirmation
# unless FORCE=1 is set in the environment.
set -euo pipefail

CONTAINER=${CONTAINER:-mcp-pgvector}
PG_USER=${PG_USER:-claude}
PG_DB=${PG_DB:-memory}
BACKUP_DIR=${BACKUP_DIR:-/shared/config/mcp-pgvector/backups}

log() { printf '[pgvector-restore] %s\n' "$*"; }
die() { printf '[pgvector-restore] ERROR: %s\n' "$*" >&2; exit 1; }

target=${1:-latest_daily}
case "$target" in
    latest_daily)
        # shellcheck disable=SC2012
        target=$(ls -1t "$BACKUP_DIR/daily"/pgvector-*.dump 2>/dev/null | head -1)
        ;;
    latest_weekly)
        # shellcheck disable=SC2012
        target=$(ls -1t "$BACKUP_DIR/weekly"/pgvector-*.dump 2>/dev/null | head -1)
        ;;
    latest_monthly)
        # shellcheck disable=SC2012
        target=$(ls -1t "$BACKUP_DIR/monthly"/pgvector-*.dump 2>/dev/null | head -1)
        ;;
esac

[ -n "$target" ] || die "no dump matched"
[ -f "$target" ] || die "not a file: $target"

log "container=$CONTAINER db=$PG_DB user=$PG_USER"
log "restore source: $target ($(du -h "$target" | cut -f1))"

if [ "${FORCE:-0}" != "1" ]; then
    printf 'This will overwrite the live DB. Type YES to proceed: '
    read -r ans
    [ "$ans" = "YES" ] || die "aborted"
fi

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q '^true$'; then
    die "container '$CONTAINER' is not running"
fi

clean_flag="--clean --if-exists"
[ "${NO_CLEAN:-0}" = "1" ] && clean_flag=""

log "running pg_restore..."
# shellcheck disable=SC2086
docker exec -i "$CONTAINER" pg_restore \
    -U "$PG_USER" -d "$PG_DB" \
    --no-owner --no-privileges \
    $clean_flag \
    < "$target"

log "done"
