# Caliber patch: strip CLAUDE_CODE_SIMPLE to fix auth

## Problem

`caliber refresh`, `caliber learn finalize`, and any Caliber command that
spawns `claude -p` fails with "Not logged in" even though the user is
authenticated and `claude auth status` returns `loggedIn: true`.

## Root cause

Two bugs interact:

1. **Claude Code bug** ([anthropics/claude-code#39453](https://github.com/anthropics/claude-code/issues/39453)):
   `claude -p` skips OAuth credential loading when `CLAUDE_CODE_SIMPLE=1`
   is set. The env var is meant to reduce the system prompt, but it also
   changes the auth pathway. Open since 2026-03-31, no fix as of 2026-04-13.

2. **Caliber bug** ([caliber-ai-org/ai-setup#114](https://github.com/caliber-ai-org/ai-setup/issues/114),
   [#138](https://github.com/caliber-ai-org/ai-setup/issues/138),
   [#152](https://github.com/caliber-ai-org/ai-setup/issues/152)):
   Caliber's `spawnClaude()` in `src/llm/claude-cli.ts` force-sets
   `CLAUDE_CODE_SIMPLE: "1"` in the child env. When Caliber runs inside
   a Claude Code hook, it also inherits `CLAUDE_CODE_SIMPLE` from the
   parent. Either way, the spawned `claude -p` sees the variable and
   refuses to authenticate.

   [PR #130](https://github.com/caliber-ai-org/ai-setup/pull/130)
   fixes this (Bug 3 of 4 in that PR) but has been in review since
   early April 2026 and is not yet merged or released.

## Patch

In `node_modules/@rely-ai/caliber/dist/bin.js`, replace:

```js
const env = { ...process.env, CLAUDE_CODE_SIMPLE: "1" };
```

with:

```js
const env = { ...process.env }; delete env.CLAUDE_CODE_SIMPLE;
```

This is the same fix proposed in PR #130.

## Apply script

```bash
CALIBER_BIN="$(npm root -g)/@rely-ai/caliber/dist/bin.js"
python3 -c "
with open('$CALIBER_BIN') as f:
    src = f.read()
old = 'const env = { ...process.env, CLAUDE_CODE_SIMPLE: \"1\" };'
new = 'const env = { ...process.env }; delete env.CLAUDE_CODE_SIMPLE;'
if old in src:
    with open('$CALIBER_BIN', 'w') as f:
        f.write(src.replace(old, new))
    print('Patched')
else:
    print('Already patched or pattern changed')
"
```

On Windows (pandorum), the easiest approach is to `scp` the patched
`bin.js` from solidPC since the patch is identical.

## When to remove

Re-check after updating Caliber. If PR #130 has been merged into the
release, the patch is no longer needed. Test with:

```bash
caliber learn finalize --force
```

If it succeeds without the patch, delete this file and the apply script.
