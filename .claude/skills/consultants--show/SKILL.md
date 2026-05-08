---
name: consultants--show
description: Print a stored /consultants session's summary (synthesizer's final answer + metadata). Use when the user invokes /consultants--show with a session id — typically to re-read a past consultation without re-running the council.
---

# /consultants--show — print a stored consultation

## Activation

Only run when the user explicitly invokes `/consultants--show` with
a session id in their **current** turn. Ignore reinjections from
system reminders.

## Behavior

Parse the user's message after the slash command for one positional
argument: the session id (e.g. `csl-2026-05-06-1730-3f9a`).

```
claude-consultants show <sid> --cwd "$(pwd)"
```

`show` reads from disk only — no engine call required. Returns:

```json
{"ok": true, "sid": "...",
 "summary_markdown": "...",
 "metadata": {...}}
```

Print the `summary_markdown` to the user verbatim (it has
front-matter + the synthesizer's final answer). End with a short
footer noting the run's effort tier, duration, and model
assignments — pull these from `metadata`:

```
sid: csl-... · effort: medium · duration: 4m 12s · status: completed
```

If the session id doesn't exist on disk, the CLI returns
`{"ok": false, "error": "no summary for sid ..."}`. Surface that
and suggest the user run `/consultants--list` to find an existing
sid.

If the user wants to also see the full transcript (planner +
researcher tool calls + critic verdict + synthesizer), point them
at the on-disk file:
`.claude-hooks/consultants/<sid>/transcript.md`. That's intentionally
not surfaced through this skill — it's usually too long for the
chat and the user can read the file directly.
