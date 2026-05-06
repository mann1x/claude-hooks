---
name: consultants
description: Run a multi-agent council consultation (planner → researcher → critic → synthesizer) on a deep / cross-cutting question via the local /consultants engine. Use when the user invokes /consultants with a query that benefits from independent specialist agents working in parallel — design audits, release-notes validation, complex bug triage, refactor risk analysis. Single-shot questions should use /get-advice instead.
---

# /consultants — multi-agent council consultation

You are about to dispatch a council of LLM specialist agents
(planner, researcher with project tools, critic, synthesizer) at a
question and surface the synthesizer's distilled answer back to the
user. The engine runs as a sibling service; consultations run in the
**background** while you continue working in the foreground.

A consultation typically takes 1–5 minutes (longer for `--effort
high` or `max`). You can — and should — keep the user productive
during that time: poll status periodically between turns, surface
per-role progress, and only block the conversation when the final
answer arrives.

## ⚠️ Activation guard — read first

Only execute the workflow below when **all** of these are true:

1. The user's **current turn** explicitly invokes `/consultants`
   with a query (e.g. `/consultants validate the release notes
   against the diff`, `/consultants refactor risk for src/foo.py`).
2. The skill is being called for the *first time* in this user
   request — not an echo from a `<system-reminder>` listing skills
   invoked earlier.

If the trigger is ambiguous, ask one short clarifying question
rather than running silently.

## Step 1 — read settings

```
claude-consultants config show
```

Parse the JSON. Note: `topology`, `effort`, `effort_budget`,
`endpoint`, per-role `enabled` + `model`, `service.mode`,
`smart_start.enabled`. If the endpoint is unreachable later, surface
that and recommend `/consultants--config` to inspect.

## Step 2 — craft the framing message

The /consultants question is `<system + user>` style: you need to
give the council enough framing to be productive. Include:

- **The user's question** verbatim or lightly tightened
- **Project context** (1-3 sentences: what this project does, the
  state of the area in question, the files / paths the council
  should focus on)
- **What "good" looks like** — e.g. "I want a yes/no on whether
  the LR schedule is reasonable for this dataset size, plus one
  concrete change if you'd alter it"
- **Where to look** — concrete `path:line` or directory pointers.
  Don't paste large code blocks; the researcher has tools.

Keep the framing under ~1 KB. The planner sees only this; everything
else is the researcher's job.

## Step 3 — start the consultation

```
claude-consultants consult --message "<your framing>" --cwd "$(pwd)"
```

Optional flags: `--effort low|medium|high|max` to override the
configured tier for this consultation. Returns:

```json
{"ok": true, "sid": "csl-2026-05-06-1730-3f9a",
 "status": "running",
 "status_url": "/v1/consult/csl-..."}
```

Save the `sid`. Tell the user briefly that you've kicked off the
council and will surface progress as it runs.

## Step 4 — poll status

```
claude-consultants status <sid>
```

Returns `{status, progress, duration_seconds, ...}`. Possible
states:

- `running` — still working. `progress` shows per-role state
  (`pending` / `in_progress` / `done`).
- `completed` — synthesizer finished. Move to Step 5.
- `failed` — something broke; `error` carries the detail.

**Polling cadence:** ~10 seconds between polls is fine. Don't
hammer it. While polling, **continue answering the user**: if they
ask other things, do them; the consultation runs in the background.

When you do poll, surface the role transitions in plain language —
e.g. "Planner done; researcher mid-investigation (3 tool calls so
far)". Don't spam the chat — one update per visible role
transition.

## Step 5 — fetch the result

Once `status: completed`:

```
claude-consultants result <sid>
```

Returns:

```json
{"ok": true, "sid": "...",
 "summary_markdown": "...",
 "metadata": {"models": {...}, "duration_seconds": ..., ...}}
```

Print the `summary_markdown` to the user (it's already formatted —
don't re-wrap or re-summarize). End with a short footer:

```
sid: csl-2026-... · effort: medium · duration: 4m 12s ·
models: planner=kimi-k2.6:cloud researcher=qwen3.5:cloud
critic=kimi-k2.6:cloud synthesizer=kimi-k2.6:cloud
```

The session is permanent — it lives at
`.claude-hooks/consultants/<sid>/{summary,transcript,metadata}.*`
and can be re-read with `/consultants--show <sid>` or read directly
from disk.

## Step 6 — handle failures

If `status: failed`:

- `error` includes "engine_python not found" or "503":
  the consultants service isn't running. Suggest the user check
  `systemctl --user status claude-hooks-consultants` (always-on
  mode) or run `python install.py` if they haven't yet.
- `error` mentions a specific role (e.g. "researcher failed: ..."):
  surface the role + error to the user. Suggest
  `/consultants--config` to switch that role's model.
- Network / cloud upstream failure: same as above; the council is
  particularly retry-prone because four roles × multiple turns
  multiplies the chance of hitting a flap.

## Notes

- **/consultants vs /get-advice**: /get-advice is a single-shot
  conversation with one model. /consultants is a multi-agent
  pipeline that takes longer but gives the synthesizer the benefit
  of independent specialist input. Reach for /consultants when
  the question benefits from a researcher actively grounding in
  project files (path:line citations) AND a critic challenging the
  evidence before the final answer is produced.
- **Effort tiers**: `low` skips the critic loop, `medium` allows
  one critic re-route, `high` allows multiple researcher rounds +
  re-routes, `max` is uncapped. Token spend scales accordingly.
- **Sessions are permanent**. Don't fire and forget — the user can
  read past consultations to compare evolutions of the same
  question. The on-disk record under `.claude-hooks/consultants/`
  is the source of truth.
