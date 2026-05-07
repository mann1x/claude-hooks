---
name: consultants--followup
description: Run a focused follow-up on a prior /consultants session. The follow-up reuses the parent's plan, research, critic verdict, and synthesizer answer — every role inherits the prior message thread, so the response is continuous with the original consultation rather than a fresh council. Use when the user invokes /consultants--followup with a question — optionally with an explicit parent sid; otherwise the most recent completed session in the project is used.
---

# /consultants--followup — iterate on a prior consultation

You are about to spawn a follow-up consultation that **reuses** the
parent session's per-role message history. The planner /
researcher / critic / synthesizer each pick up exactly where they
left off, so this is materially different from running
`/consultants` again with a related question — the follow-up's
answer is continuous with the parent's, the way turn N+1 of a
warm chat would be.

Use this when the user wants to:

- Push back on the synthesizer's recommendation ("the researcher
  missed file X — re-evaluate with that").
- Drill into a specific point from the parent answer ("expand on
  the trade-off you flagged in section 3").
- Ask a related question that depends on the parent's grounding
  ("given that analysis, what's the migration path?").

For a fresh question that doesn't depend on prior context, use
`/consultants` instead — a follow-up on an unrelated question
just confuses the council.

## ⚠️ Activation guard — read first

Only execute the workflow below when **all** of these are true:

1. The user's **current turn** explicitly invokes
   `/consultants--followup` with a follow-up question (e.g.
   `/consultants--followup what about edge case X`,
   `/consultants--followup csl-2026-... drill into point 3`).
2. The skill is being called for the *first time* in this user
   request — not an echo from a `<system-reminder>` listing skills
   invoked earlier.

If the trigger is ambiguous, ask one short clarifying question
rather than running silently.

## Step 1 — resolve the parent sid

Parse the user's message after the slash command. Look for a
positional sid argument matching the pattern
`csl-<YYYY-MM-DD>-<HHMM>-<hex>` at the start of the args. The
remaining text is the follow-up message.

**If the user provided a sid:** use it directly.

**If no sid is present and a previous /consultants or
/consultants--show in the current Claude Code session printed a
session id in its footer:** use that one. Tell the user which sid
you picked.

**If no sid is available:** run

```
claude-consultants list --cwd "$(pwd)" --limit 10
```

Surface the JSON `sessions` array as a compact numbered list
(newest first, with sid + status + effort + question preview).
If exactly one session is `completed`, default to it and announce
that choice. If multiple are completed, AskUserQuestion which one
to follow up on. If the list is empty, say so plainly and suggest
the user start a new consultation with `/consultants`.

## Step 2 — check whether the parent is warm

Optional but worth a single quick poll for user feedback:

```
claude-consultants list-open
```

If the parent sid is in the warm pool, follow-up will resolve in
sub-second; if not, it auto-reopens from disk artifacts (~5-10 s
cold start). Mention this to the user so a 10-second pause doesn't
look like a hang. Don't gate on it — the next step handles both
paths transparently.

## Step 3 — fire the follow-up

```
claude-consultants follow-up <parent_sid> --message "<follow-up text>" --cwd "$(pwd)"
```

Optional flags:

- `--effort low|medium|high|max|xmedium|xhigh|xmax` — override the
  effort tier for this follow-up alone (defaults to the parent's
  tier). Use the same picking logic as `/consultants`: bump to
  `high`/`xhigh` only if the user explicitly asks for a deeper
  pass; otherwise inherit.

Returns:

```json
{"ok": true, "sid": "csl-2026-...", "parent_sid": "...",
 "status": "running", "status_url": "/v1/consult/csl-..."}
```

Save the new `sid`. Tell the user briefly that the follow-up is
running and you'll surface progress.

## Step 4 — poll status

Same as `/consultants` step 4:

```
claude-consultants status <new_sid>
```

Poll every ~10 s. Surface per-role progress only on visible
transitions ("planner: done; researcher: 2 tool calls so far").
Don't spam.

## Step 5 — fetch the result

Once `status: completed`:

```
claude-consultants result <new_sid>
```

Print the `summary_markdown` verbatim. End with an extended footer
that explicitly threads the lineage:

```
sid: csl-2026-...-NEW · parent: csl-2026-...-OLD · effort: medium ·
duration: 1m 47s · models: planner=... researcher=... critic=...
synthesizer=...
```

The lineage is what tells the user this answer is continuous with
the prior consultation — show it every time.

The follow-up is itself a permanent session under
`.claude-hooks/consultants/<new_sid>/`, so it can be the parent of
a further follow-up. Chains of follow-ups are fine; each one
inherits all prior turns from the same role thread.

## Step 6 — handle failures

- `error: "parent session not found"` — the sid was wrong or the
  parent's on-disk artifacts were pruned. Suggest
  `/consultants--list` to find a valid sid.
- `error: "engine_python not found"` / connection refused — the
  consultants service isn't running. Same handling as
  `/consultants` step 6.
- Mid-flight role failure — surface the role + error. The parent
  session is unaffected; the follow-up is its own session and
  can simply be re-run.

## Notes

- **Follow-up vs new consultation**: if the user's question would
  benefit from a fresh planner pass (different problem domain,
  different files in scope), use `/consultants` not this skill.
  The signal: does the question reference the parent's findings or
  recommendations? If yes, follow-up. If no, fresh.
- **Chains are cheap**: a follow-up on a follow-up costs only the
  marginal turn — the warm-engine path skips planner re-grounding
  entirely. This is the intended workflow for iterative refinement
  on a single deep topic.
- The CLI's `claude-consultants close <sid>` releases warm engine
  memory but **does not** prevent a later follow-up — closed
  sessions auto-reopen from disk on the next `follow-up`. So
  `close` is purely a memory-pressure tool, not a "lock the
  session" tool.
