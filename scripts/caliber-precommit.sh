#!/bin/sh
# Caliber: sync agent configs on commit (non-blocking).
#
# This hook is intentionally fire-and-forget:
#   - `caliber refresh` calls an LLM and can take 30-70s even when no
#     docs change. We cap it (via GNU timeout if available) at 30s
#     and run it in the background so `git commit` returns
#     immediately. Any refreshed docs land in the NEXT commit.
#   - `caliber learn finalize` is NOT run here - SessionEnd hooks
#     already run `caliber learn finalize --auto`, and running the
#     full (non-auto) version on the commit path was the single
#     biggest contributor (~10 min on a 240-event session).
#   - CALIBER_CLAUDE_CLI_TIMEOUT_MS=60000 bounds the inner Claude CLI
#     call so a slow LLM can't pin the background process longer than
#     ~60s even if the outer `timeout` isn't available (Windows Git
#     Bash lacks GNU coreutils timeout — its `timeout` is a sleep).
#
# The keyword "caliber" remains below so tools that grep for it
# (including Caliber's own setup detection) continue to see an
# active hook.

# caliber:pre-commit:start
if [ -x "caliber" ] || command -v "caliber" >/dev/null 2>&1; then
  mkdir -p .caliber
  # Pick a GNU-compatible `timeout` binary if available. Windows'
  # C:\Windows\system32\timeout.exe is a sleep, NOT a command wrapper,
  # so we skip it and fall back to the Claude-CLI env timeout.
  _cal_tmo=""
  if [ -x /usr/bin/timeout ]; then
    _cal_tmo="/usr/bin/timeout 30"
  elif command -v gtimeout >/dev/null 2>&1; then
    _cal_tmo="gtimeout 30"
  fi
  echo "caliber: refreshing docs in background..."
  (
    CALIBER_CLAUDE_CLI_TIMEOUT_MS=60000 \
      $_cal_tmo caliber refresh --quiet \
        >.caliber/refresh-hook.log 2>&1 || true
  ) </dev/null >/dev/null 2>&1 &
fi
# caliber:pre-commit:end
