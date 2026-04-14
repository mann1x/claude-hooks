---
name: wrapup
description: Produce a complete, restore-ready state summary of the current session. Use this when the user wants to compact context, pause the session, or hand off work — especially with phrases like "compact the context", "save state", "wrap up", "let's continue another time", "we will continue", "context is running low", "/wrapup", or any formulation that asks to end/pause the current conversation so it can be resumed later.
---

# /wrapup — Session State Summary

Produce a complete, restore-ready state summary so the user can compact
the context or end the session and resume later with everything intact.

## When to use

Invoke this skill whenever the user indicates they want to stop or
compact the session, in any formulation. Examples:

- "compact the context", "context is running low", "need to compact"
- "wrap up", "wrap up for now", "let's close this session"
- "save state", "state summary", "session summary"
- "continue another time", "we'll continue", "pick this up later"
- Direct: "/wrapup" or "/wrapup <extra instructions>"

If the user's `/wrapup` invocation includes extra text (e.g.
`/wrapup focus on the test coverage work only`), treat that text as
**additional filter / emphasis instructions** and apply them to the
summary — narrow the scope, highlight what they asked for, or skip
sections they don't care about.

## The stop_guard escape

The `stop_guard` hook (when enabled) blocks the assistant from stopping
on phrases like "wrap up", "good stopping point", "continue in the
next session". `/wrapup` is **safe** to invoke anyway because stop_guard
checks the user's most recent message for wrap-up markers and bypasses
the hook when one is present. `/wrapup` is itself in the default marker
list, so invoking this skill will not be blocked.

If stop_guard still fires for some reason, the user can temporarily
disable it with `hooks.stop_guard.enabled: false` in
`config/claude-hooks.json`, or set `hooks.stop_guard.skip_on_user_wrap_up:
false` to disable only the user-intent escape (not recommended).

## Required sections

Produce the summary in this exact order. Skip a section only if it
genuinely doesn't apply — don't hide relevant state just to be brief.

### 1. Session snapshot (always include)

One paragraph, plain prose:
- What the session was about (the initial ask)
- The current shape of work at the moment of wrap-up
- Commit count and HEAD hash for each repo touched

### 2. Session achievements

Bullet list of concrete outcomes actually landed this session:
- Commits authored (hash + one-line subject)
- Tests added (count and what they cover)
- Files created / significantly modified (short list)
- External state changes (pod reindexed, hook installed, service
  restarted, config edited, package upgraded, etc.)

### 3. Open items

Anything in progress, unresolved, or waiting on the user:

- **In-progress work**: code paths / branches / drafts not yet
  committed, with exact file paths and what needs to happen next
- **Unresolved bugs / failures**: short description + last known
  error message + file:line where it manifests
- **User questions / decisions pending**: anything the assistant
  asked but never got an answer on

### 4. Next items

Ordered list of what to do next, with enough context to resume
cold without rereading the whole session:
- Exact command to run, or
- Exact file:line to edit, or
- Exact question to ask the user first

### 5. Plans in use or referenced

If the session worked from or updated any plan documents:
- `docs/PLAN-*.md` files that were touched — note which phase / item
  was reached
- Skills, agents, or external docs that were followed
- Memory entries that were created or updated (`memory/*.md`)

### 6. Active monitorings to re-establish

Anything the assistant was watching that needs to restart on resume:
- Background tasks (`run_in_background: true`) — mention the command
  and the output file path if still relevant
- `ScheduleWakeup` timers still pending
- Scheduled crons (`CronCreate`) relevant to this work
- `Monitor` streams the next session should re-attach to

### 7. Pods / remote hosts status (if actively engaged)

For every remote host the session actively touched:
- Host name + user + key path
- What state we left it in (services running, files modified, env
  changes applied)
- Any cleanup the next session should do first

### 8. Restore checklist

A copy-paste-friendly list of commands / reads the next session
should run first to get back to this exact state:

```
git log --oneline origin/main -10
python3 -m pytest tests/ --tb=line -q
cat docs/PLAN-*.md | head -N  # if plan work in progress
claudemem status             # if claudemem-backed
```

Include anything project-specific that primes the mental model.

## Output location

By default, the summary goes **inline in the chat** so the user sees
it before the context is compacted. Do NOT write it to a file unless
the user asks for one — the whole point is that it lives in the next
session's context.

If the user does ask for a file, save to
`.wolf/wrapup-YYYY-MM-DDTHH-MM.md` (inside the OpenWolf dir if one
exists, otherwise `docs/wrapup/YYYY-MM-DDTHH-MM.md`). Prefix the
filename with the session branch if on a feature branch.

## Tone and format

- Markdown headings for each section, bullet lists inside
- No preamble (just start with section 1)
- No sign-off (no "let me know if…", no "happy wrapping up")
- Focus on **fidelity** — if something is unclear, say "unclear" or
  "needs user confirmation", don't paper over it

## Examples of good vs bad output

Good — concrete, resumable:
```markdown
### 3. Open items

- `tests/test_reflect.py` — file exists but is unverified. Delete or
  port to the new `fake_provider` fixture. `rm tests/test_reflect.py`
  to discard; otherwise `python3 -m pytest tests/test_reflect.py -v`.
- Phase 4 of `docs/PLAN-test-coverage.md` (reflect, consolidate,
  instincts) — instincts DONE (13 tests), reflect WIP, consolidate
  not started.
```

Bad — vague, won't help next session:
```markdown
### 3. Open items

- Some test files need work.
- There's a plan we were following.
```
