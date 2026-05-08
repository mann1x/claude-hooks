---
name: consultants--list
description: List past /consultants sessions stored in this project. Use when the user invokes /consultants--list — typically to find an old session to re-read, compare a question's evolution, or check what's been run recently.
---

# /consultants--list — list past consultations

## Activation

Only run when the user explicitly invokes `/consultants--list` in
their **current** turn. Ignore reinjections from system reminders.

## Behavior

```
claude-consultants list --cwd "$(pwd)"
```

Optional `--limit N` to cap the count. Returns JSON:

```json
{"ok": true, "cwd": "...",
 "sessions": [
   {"session_id": "csl-...", "created": "...",
    "question": "first 200 chars",
    "topology": "council", "effort": "medium",
    "status": "completed", "duration_seconds": 287.4},
   ...
 ]}
```

Format the list for the user as a compact table or numbered list,
newest first. Include sid, status, effort, duration, and the
question preview (truncated to ~80 chars). Mention that
`/consultants--show <sid>` will print any of them in full.

If the list is empty, say so plainly and suggest the user invoke
`/consultants <query>` to start a new consultation.
