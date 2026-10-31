#!/bin/sh
# Install the fast caliber pre-commit block into .git/hooks/pre-commit.
#
# Caliber's default pre-commit hook runs `caliber refresh` and `caliber
# learn finalize` (LLM calls) synchronously, which can block `git commit`
# for 20 minutes or more. scripts/caliber-precommit.sh holds a
# non-blocking block instead: the refresh runs in the background with
# inactivity (not wall-clock) bounds, and `learn finalize` is left to the
# SessionEnd hook. See the comments inside that block.
#
# The block is MERGED, never copied over the hook: only the span between
# `# caliber:pre-commit[:vN]:start` and `...:end` is replaced (caliber's
# own `caliber hooks --install` block included); with no such span the
# block is appended, so anything earlier — a secret gate above all — keeps
# running first. A hook is created (with a shebang) only when absent. The
# old installer ended in `cp`, which deleted a secret gate already in the
# hook (backup_models #1940).
#
# Usage:
#   sh scripts/install-caliber-hook.sh        # install / update
#   sh scripts/install-caliber-hook.sh --dry  # show what would happen
#
# Reversible: a hook it changes is backed up to
# .git/hooks/pre-commit.bak-<timestamp> first.

set -u

# --- locate repo root
if ! repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "error: not inside a git working tree" >&2
  exit 2
fi
cd "$repo_root" || exit 2
script_dir="$(cd "$(dirname "$0")" && pwd)"

src="$script_dir/caliber-precommit.sh"
hooks_dir="$(git rev-parse --git-path hooks)"
dst="$hooks_dir/pre-commit"

if [ ! -f "$src" ]; then
  echo "error: $src not found" >&2
  exit 2
fi

dry_run=0
if [ "${1:-}" = "--dry" ] || [ "${1:-}" = "--dry-run" ]; then
  dry_run=1
fi

START='^# caliber:pre-commit(:v[0-9]+)?:start[ \t]*$'
END='^# caliber:pre-commit(:v[0-9]+)?:end[ \t]*$'
# grep -E reads `\t` in a bracket as a backslash and a t: its own patterns
GSTART='^# caliber:pre-commit(:v[0-9]+)?:start'
GEND='^# caliber:pre-commit(:v[0-9]+)?:end'

tmp_dir="$(mktemp -d 2>/dev/null || mktemp -d -t calhook)"
trap 'rm -rf "$tmp_dir"' EXIT INT TERM
block="$tmp_dir/block"
merged="$tmp_dir/merged"

awk -v s="$START" -v e="$END" '$0 ~ s {f=1} f {print} $0 ~ e {f=0}' "$src" >"$block"
if ! grep -Eq "$GSTART" "$block" || ! grep -Eq "$GEND" "$block"; then
  echo "error: $src has no caliber:pre-commit start/end block" >&2
  exit 2
fi

if [ -e "$dst" ]; then
  # Every start needs its end, or replacing the span would swallow the
  # rest of the hook.
  if ! awk -v s="$START" -v e="$END" '
        $0 ~ s { if (open) bad = 1; open = 1 }
        $0 ~ e { if (!open) bad = 1; open = 0 }
        END { exit (bad || open) }' "$dst"; then
    echo "error: $dst has an unmatched caliber:pre-commit marker — fix it by hand; nothing changed" >&2
    exit 1
  fi
  awk -v s="$START" -v e="$END" -v blockfile="$block" '
    BEGIN { while ((getline l < blockfile) > 0) blk = blk l "\n" }
    $0 ~ s { if (!done) { printf "%s", blk; done = 1 } skip = 1; next }
    skip && $0 ~ e { skip = 0; next }
    skip { next }
    { print; last = $0 }
    END { if (!done) { if (NR > 0 && last != "") printf "\n"; printf "%s", blk } }
  ' "$dst" >"$merged"
  action="update"
else
  { echo "#!/bin/sh"; cat "$block"; } >"$merged"
  action="install"
fi

if [ -e "$dst" ] && cmp -s "$merged" "$dst"; then
  echo "caliber hook already up to date at $dst — nothing to do"
  exit 0
fi

# An `exit` / `exec` at the top level before the block ends the hook
# before caliber runs; a secret gate after it runs only after caliber.
warn="$(awk -v s="$START" '
  $0 ~ s { exit }
  /^(exit|exec)([ \t]|$)/ { print "warning: line " NR " (" $0 ") ends the hook before the caliber block" }
' "$merged")"
[ -n "$warn" ] && echo "$warn" >&2
if grep -q "claude-hooks secret gate" "$merged"; then
  gate_line="$(grep -n "claude-hooks secret gate" "$merged" | head -1 | cut -d: -f1)"
  cal_line="$(grep -En "$GSTART" "$merged" | head -1 | cut -d: -f1)"
  if [ "$gate_line" -gt "$cal_line" ]; then
    echo "warning: the secret gate (line $gate_line) runs after the caliber block (line $cal_line)" >&2
  fi
fi

ts="$(date +%Y%m%d-%H%M%S)"
backup="${dst}.bak-${ts}"

if [ "$dry_run" -eq 1 ]; then
  echo "dry run: would ${action} the caliber block in $dst"
  [ -e "$dst" ] && echo "dry run: would back up existing hook to $backup"
  exit 0
fi

mkdir -p "$hooks_dir"
if [ -e "$dst" ]; then
  cp "$dst" "$backup" || {
    echo "error: failed to back up $dst to $backup" >&2
    exit 1
  }
  echo "backed up existing hook -> $backup"
fi

cat "$merged" >"$dst" || {
  echo "error: failed to write $dst" >&2
  exit 1
}
chmod +x "$dst"

echo "caliber pre-commit block ${action}d in $dst (the rest of the hook kept)"
