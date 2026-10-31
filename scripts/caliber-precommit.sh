#!/bin/sh
# caliber:pre-commit:start
# Caliber: sync agent configs on commit (non-blocking).
# Installed by scripts/install-caliber-hook.sh, which merges this block
# into .git/hooks/pre-commit between its markers and never touches the
# rest (a secret gate above it keeps running first).
#
# - `caliber refresh` runs in the background, so `git commit` returns at
#   once and refreshed docs land in the NEXT commit. A refresh of this
#   repo measured ~110 s, so there is no wall-clock cap: the old
#   `timeout 30` and CALIBER_CLAUDE_CLI_TIMEOUT_MS=60000 killed every one
#   (AGENTS.md sat at its May content, the state 73 commits behind HEAD).
#   What bounds it is stream inactivity, which stops a stalled call
#   without cutting a healthy long generation.
# - `--quiet` only on a caliber that exempts git hooks from its
#   Claude-session cascade guard (ai-setup #251, detected by the
#   GIT_INDEX_FILE check in its build). Elsewhere, from a Claude Code
#   session (CLAUDECODE=1, stdin not a TTY), `--quiet` makes the refresh
#   return at once having done nothing.
# - `caliber learn finalize` is not run here: SessionEnd hooks run the
#   `--auto` version, and the full one cost ~10 min per commit.
#
# Working when `lastRefreshSha` in .caliber/.caliber-state.json tracks
# HEAD; output is in .caliber/refresh-hook.log.
if [ -x "caliber" ] || command -v "caliber" >/dev/null 2>&1; then
  mkdir -p .caliber
  _cal_quiet=""
  _cal_bin="$(command -v caliber 2>/dev/null)"
  _cal_real="$(readlink -f "$_cal_bin" 2>/dev/null || echo "$_cal_bin")"
  if [ -n "$_cal_real" ] && grep -q GIT_INDEX_FILE "$_cal_real" 2>/dev/null; then
    _cal_quiet="--quiet"
  fi
  echo "caliber: refreshing docs in background..."
  (
    CALIBER_CLAUDE_CLI_TIMEOUT_MS=3600000 \
    CALIBER_GENERATION_TIMEOUT_MS=1800000 \
    CALIBER_STREAM_INACTIVITY_TIMEOUT_MS=600000 \
      caliber refresh $_cal_quiet >.caliber/refresh-hook.log 2>&1 || true
  ) </dev/null >/dev/null 2>&1 &
fi
# caliber:pre-commit:end
