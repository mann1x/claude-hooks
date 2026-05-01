# OpenWolf upstream issue draft — `_managedBy` tag

Repo: <https://github.com/cytostack/openwolf>

## Title
Hook entries lack `_managedBy` tag, get dropped when Claude Code rewrites `settings.json`

## Body

### Environment
- **OpenWolf:** 1.0.4
- **Claude Code:** 2.1.121–2.1.126 (any recent build)
- **OS:** Linux + Windows (reproduced on both)

### Summary
The 6 hook entries OpenWolf installs into `~/.claude/settings.json` (or `.claude/settings.json`) carry no provenance tag. When Claude Code rewrites the file as part of an unrelated command — `/effort`, `/config`, plugin toggling, etc. — entries that don't carry a `_managedBy` field appear to be dropped silently. This is consistent with how Claude Code's own merge logic works: it preserves entries it recognizes as managed by a third party (e.g. `claude-hooks` tags everything it owns with `_managedBy: "claude-hooks"`), and it drops everything else.

The result: a working OpenWolf install can be silently de-wired by typing `/effort medium` once. The `.wolf/hooks/*.js` files stay on disk, but Claude Code stops invoking them.

### Steps to reproduce
1. `openwolf init` in a project, verify the 6 hooks fire (SessionStart, Stop, Pre/PostToolUse Read + Write|Edit|MultiEdit).
2. In Claude Code, run `/effort high` (or any command that mutates `effortLevel`).
3. Inspect `~/.claude/settings.json` — the `node "$CLAUDE_PROJECT_DIR/.wolf/hooks/*.js"` entries are gone. The claude-hooks-tagged entries (if any) are still there.
4. Hook callbacks no longer fire on subsequent sessions.

### Proposed fix

Two small changes in `src/cli/init.ts` (and the matching block in `src/cli/update.ts`):

1. **Tag every entry in `HOOK_SETTINGS`** with `_managedBy: "openwolf"` so Claude Code recognizes them as owned and preserves them through its own settings round-trips.

2. **Tighten the dedupe filter in `replaceOpenWolfHooks`** to match by tag in addition to the `.wolf/hooks/` substring — defensive against future path schema changes.

```diff
 const HOOK_SETTINGS = {
   hooks: {
     SessionStart: [
       {
         matcher: "",
         hooks: [
           {
             type: "command",
             command: 'node "$CLAUDE_PROJECT_DIR/.wolf/hooks/session-start.js"',
             timeout: 5,
+            _managedBy: "openwolf",
           },
         ],
       },
     ],
     // ... same _managedBy: "openwolf" added to every hook object in
     //     PreToolUse Read + Write|Edit|MultiEdit,
     //     PostToolUse Read + Write|Edit|MultiEdit,
     //     Stop.
   },
 };

 function replaceOpenWolfHooks(existing, hookSettings) {
   const merged = { ...existing };
   if (!merged.hooks) merged.hooks = {};
   const hooks = merged.hooks;
   for (const [event, newMatchers] of Object.entries(hookSettings.hooks)) {
     if (!hooks[event]) hooks[event] = [];
-    hooks[event] = hooks[event].filter((entry) => {
-      const isOpenWolfHook = entry.hooks?.some(
-        (h) => h.command && h.command.includes(".wolf/hooks/")
-      );
-      return !isOpenWolfHook;
-    });
+    hooks[event] = hooks[event].filter((entry) => {
+      const isOpenWolfHook = entry.hooks?.some(
+        (h) =>
+          h._managedBy === "openwolf" ||
+          (h.command && h.command.includes(".wolf/hooks/"))
+      );
+      return !isOpenWolfHook;
+    });
     for (const matcher of newMatchers) {
       hooks[event].push(matcher);
     }
   }
   return merged;
 }
```

### Why this works
Claude Code's settings round-tripper preserves any entry whose hooks
carry a `_managedBy` field — that's how `claude-hooks` and other
third-party hook providers survive `/effort`, `/config`, and similar
rewrites. Adding the tag costs one line per entry and is fully
backward-compatible: an old install missing the tag is still picked up
by the substring check.

### Workaround in the meantime
Add the tag manually in `~/.claude/settings.json` (or `.claude/settings.json`):

```json
{
  "type": "command",
  "command": "node \"$CLAUDE_PROJECT_DIR/.wolf/hooks/session-start.js\"",
  "timeout": 5,
  "_managedBy": "openwolf"
}
```
