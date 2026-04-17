#!/bin/sh
# Install the fast caliber pre-commit hook into .git/hooks/pre-commit.
#
# Caliber's default pre-commit hook runs `caliber refresh` (an LLM call)
# and `caliber learn finalize` (another LLM call over accumulated session
# events) synchronously. On large sessions this can block `git commit`
# for 20 minutes or more. This installer replaces that hook with a
# portable, non-blocking version that:
#
#   - backgrounds `caliber refresh` so the commit returns instantly
#     (refreshed docs land in the NEXT commit — a fair trade)
#   - drops `caliber learn finalize` (the SessionEnd Claude Code hook
#     already runs the `--auto` version)
#   - bounds the inner Claude CLI call at 60 s via
#     CALIBER_CLAUDE_CLI_TIMEOUT_MS
#   - wraps the refresh in GNU `timeout 30` when available
#     (skipped on Windows Git Bash where `timeout.exe` is a *sleep*
#     command with different semantics)
#
# Measured on a 240-event session:
#   default hook: ~20 min per commit
#   this hook:    ~0.7 s per commit (1700x faster)
#
# Usage:
#   sh scripts/install-caliber-hook.sh        # install / update
#   sh scripts/install-caliber-hook.sh --dry  # show what would happen
#
# Reversible: any existing .git/hooks/pre-commit is backed up to
# .git/hooks/pre-commit.bak-<timestamp> before being replaced.

set -u

# --- locate repo root
if ! repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "error: not inside a git working tree" >&2
  exit 2
fi
cd "$repo_root" || exit 2

src="scripts/caliber-precommit.sh"
dst=".git/hooks/pre-commit"

if [ ! -f "$src" ]; then
  echo "error: $src not found — run this from a clone that has the script checked in" >&2
  exit 2
fi

dry_run=0
if [ "${1:-}" = "--dry" ] || [ "${1:-}" = "--dry-run" ]; then
  dry_run=1
fi

action="install"
if [ -e "$dst" ]; then
  if cmp -s "$src" "$dst" 2>/dev/null; then
    echo "caliber hook already up to date at $dst — nothing to do"
    exit 0
  fi
  action="update"
fi

ts="$(date +%Y%m%d-%H%M%S)"
backup="${dst}.bak-${ts}"

if [ "$dry_run" -eq 1 ]; then
  echo "dry run: would ${action} $dst"
  [ -e "$dst" ] && echo "dry run: would back up existing hook to $backup"
  echo "dry run: would chmod +x $dst"
  exit 0
fi

mkdir -p .git/hooks

if [ -e "$dst" ]; then
  cp "$dst" "$backup" || {
    echo "error: failed to back up $dst to $backup" >&2
    exit 1
  }
  echo "backed up existing hook -> $backup"
fi

cp "$src" "$dst" || {
  echo "error: failed to copy $src -> $dst" >&2
  exit 1
}
chmod +x "$dst"

echo "caliber pre-commit hook ${action}ed at $dst"
echo "next commit will run the fast (non-blocking) version."
