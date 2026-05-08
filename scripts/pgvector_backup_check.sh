#!/bin/bash
# pgvector_backup_check.sh — backup-validity canary.
#
# Picks the most recent dump in each tier (daily/weekly/monthly) and
# runs two layers of validation:
#   1. pg_restore -l                     (fast: TOC header + metadata)
#   2. pg_restore -f /dev/null           (thorough: reads every byte,
#                                         emits all SQL to /dev/null,
#                                         no DB side effects)
#
# Layer 2 catches mid-file corruption / truncation that the TOC scan
# misses. Both run inside the mcp-pgvector container so we use the
# same pg_restore version that wrote the dump.
#
# Exits non-zero if any tier fails — systemd OnFailure= can wire that
# into a notification.
#
# Tunables (env or systemd Environment=):
#   CONTAINER         mcp-pgvector
#   BACKUP_DIR        /shared/config/mcp-pgvector/backups
#   CHECK_TIERS       "daily weekly monthly"
#   FAIL_ON_EMPTY     0    (1 = fail when a tier dir has no dumps)
set -euo pipefail

CONTAINER=${CONTAINER:-mcp-pgvector}
BACKUP_DIR=${BACKUP_DIR:-/shared/config/mcp-pgvector/backups}
CHECK_TIERS=${CHECK_TIERS:-"daily weekly monthly"}
FAIL_ON_EMPTY=${FAIL_ON_EMPTY:-0}

log() { printf '[pgvector-check] %s\n' "$*"; }
warn() { printf '[pgvector-check] WARN: %s\n' "$*" >&2; }
err() { printf '[pgvector-check] FAIL: %s\n' "$*" >&2; }

command -v docker >/dev/null 2>&1 || { err "docker not found"; exit 2; }

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q '^true$'; then
    err "container '$CONTAINER' is not running"
    exit 2
fi

failures=0
total_checked=0

check_one() {
    local f="$1"
    local size_h toc_lines start elapsed
    size_h=$(du -h "$f" | cut -f1)

    # Layer 1: TOC scan. Returns the number of TOC entries on success.
    if ! toc_lines=$(docker exec -i "$CONTAINER" pg_restore -l < "$f" 2>/dev/null \
                     | grep -c '^[0-9]'); then
        err "TOC scan failed for $f"
        return 1
    fi
    if [ "$toc_lines" -lt 1 ]; then
        err "TOC empty for $f (likely corrupt header)"
        return 1
    fi

    # Layer 2: full read — emit SQL to /dev/null.
    start=$(date +%s)
    if ! docker exec -i "$CONTAINER" pg_restore -f /dev/null < "$f" \
            >/dev/null 2>&1; then
        err "full-read failed for $f (mid-file corruption?)"
        return 1
    fi
    elapsed=$(( $(date +%s) - start ))

    log "OK  $(basename "$f")  size=$size_h  toc_entries=$toc_lines  read=${elapsed}s"
    return 0
}

for tier in $CHECK_TIERS; do
    tier_dir="$BACKUP_DIR/$tier"
    if [ ! -d "$tier_dir" ]; then
        warn "$tier: directory missing ($tier_dir)"
        [ "$FAIL_ON_EMPTY" = "1" ] && failures=$((failures + 1))
        continue
    fi

    latest=$(find "$tier_dir" -maxdepth 1 -name 'pgvector-*.dump' -type f \
             -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)
    if [ -z "$latest" ]; then
        warn "$tier: no dumps in $tier_dir"
        [ "$FAIL_ON_EMPTY" = "1" ] && failures=$((failures + 1))
        continue
    fi

    if check_one "$latest"; then
        total_checked=$((total_checked + 1))
    else
        failures=$((failures + 1))
    fi
done

if [ "$failures" -gt 0 ]; then
    err "$failures tier(s) failed validation; $total_checked passed"
    exit 1
fi

log "all $total_checked tier(s) validated"
