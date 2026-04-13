#!/usr/bin/env bash
# Apply the CLAUDE_CODE_SIMPLE auth patch to Caliber.
# Safe to re-run — idempotent.
# See caliber-claude-code-simple-auth.md for context.

set -euo pipefail

CALIBER_BIN="$(npm root -g)/@rely-ai/caliber/dist/bin.js"

if [ ! -f "$CALIBER_BIN" ]; then
  echo "Caliber not found at $CALIBER_BIN"
  exit 1
fi

python3 -c "
with open('$CALIBER_BIN') as f:
    src = f.read()
old = 'const env = { ...process.env, CLAUDE_CODE_SIMPLE: \"1\" };'
new = 'const env = { ...process.env }; delete env.CLAUDE_CODE_SIMPLE;'
if old in src:
    with open('$CALIBER_BIN', 'w') as f:
        f.write(src.replace(old, new))
    print('Patched:', '$CALIBER_BIN')
elif 'delete env.CLAUDE_CODE_SIMPLE' in src:
    print('Already patched')
else:
    print('WARNING: pattern not found — Caliber may have changed. Check manually.')
    exit(1)
"
