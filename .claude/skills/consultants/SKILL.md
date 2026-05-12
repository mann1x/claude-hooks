---
name: consultants
description: Multi-agent council engine (planner → researcher → critic → synthesizer). Default verb is `ask <question>` (also implicit — `/consultants <question>` works). Subcommands — `ask` runs a fresh council on a question; `followup [sid] <question>` iterates on a prior consult (warm reuse of plan/research/critic); `list` shows past sessions; `show <sid>` re-reads a stored summary; `config [args...]` walks the role/model/effort/service-mode dialog. Use when a question benefits from independent specialist agents working in parallel — design audits, release-notes validation, complex bug triage, refactor risk analysis. For single-shot questions use /get-advice instead.
---

# /consultants — multi-agent council dispatcher

You are the dispatcher for a council of LLM specialist agents
(planner, researcher with project tools, critic, synthesizer) and
its supporting management commands. The engine runs as a sibling
service on `127.0.0.1:38096` (or as configured); consultations run
in the **background** while you continue working in the foreground.

## ⚠️ Activation guard — read first

Only execute when **both** are true:

1. The user's **current** turn explicitly invokes `/consultants`.
2. This is the *first* call in the current user request — not an
   echo from a `<system-reminder>` listing skills invoked earlier.

If the trigger is ambiguous, ask one short clarifying question
rather than running silently.

## Verb dispatch

Parse the first whitespace-separated token of the user's args
after `/consultants`:

| First token  | Verb       | Action                                                         |
|--------------|------------|----------------------------------------------------------------|
| `ask`        | ask        | Drop the verb; run the council on the rest as the question.    |
| `followup`   | followup   | Drop the verb; run **followup** flow on the rest.              |
| `list`       | list       | Run **list** flow.                                             |
| `show`       | show       | Drop the verb; the rest is the sid.                            |
| `config`     | config     | Drop the verb; run **config** flow with the rest as sub-args.  |
| anything else (incl. empty) | ask | Implicit ask: treat the **entire** arg as the question. |

Examples:

- `/consultants what's wrong with foo` → implicit **ask**
- `/consultants ask validate release notes` → explicit **ask**
- `/consultants followup csl-2026-... drill into point 3` → **followup** with explicit parent sid
- `/consultants followup what about edge case X` → **followup**, auto-pick most-recent parent
- `/consultants list --limit 20` → **list**
- `/consultants show csl-2026-05-06-1730-3f9a` → **show**
- `/consultants config` → **config** (no sub-args → walk the dialog)
- `/consultants config set-effort high` → **config** with passthrough args

---

## ask — run a fresh council consultation

A consultation typically takes 1–5 minutes (longer for `high` or
`max` effort). Keep the user productive during that time: poll
status periodically, surface per-role progress on visible
transitions, only block when the synthesizer's answer arrives.

### 1. Read engine settings

```
claude-consultants config show
```

Parse the JSON for `topology`, `effort`, `effort_budget`,
`endpoint`, per-role `enabled` + `model`, `service.mode`. If the
endpoint is unreachable later, surface and recommend
`/consultants config` to inspect.

### 2. Craft the framing message

Include:

- **The user's question** verbatim or lightly tightened.
- **Project context** (1–3 sentences: what this project does, the
  state of the area, files / paths the council should focus on).
- **What "good" looks like** — e.g. "yes/no on whether the LR
  schedule is reasonable, plus one concrete change if you'd alter it".
- **Where to look** — concrete `path:line` or directory pointers.
  Don't paste large code blocks; the researcher has tools.

Keep framing under ~1 KB.

### 3. Start the consultation

```
claude-consultants consult --message "<your framing>" --cwd "$(pwd)"
```

Optional `--effort low|medium|high|max|xmedium|xhigh|xmax` to
override the configured tier for this call. Returns:

```json
{"ok": true, "sid": "csl-2026-05-06-1730-3f9a",
 "status": "running", "status_url": "/v1/consult/csl-..."}
```

Save the `sid`. Tell the user briefly you've kicked off the council
and will surface progress.

### 4. Poll status

```
claude-consultants status <sid>
```

Returns `{status, progress, duration_seconds, ...}`. States:

- `running` — `progress` shows per-role state. Continue answering
  the user; the consultation runs in the background.
- `completed` — move to step 5.
- `failed` — `error` carries the detail; jump to failure handling.

Cadence: ~10s between polls. Surface role transitions in plain
language ("Planner done; researcher mid-investigation, 3 tool calls
so far"). One update per visible transition, no spam.

### 5. Fetch the result

```
claude-consultants result <sid>
```

Print `summary_markdown` verbatim (it's already formatted; don't
re-wrap). End with a footer:

```
sid: csl-2026-... · effort: medium · duration: 4m 12s ·
models: planner=kimi-k2.6:cloud researcher=qwen3.5:cloud
critic=kimi-k2.6:cloud synthesizer=kimi-k2.6:cloud
```

The session is permanent at
`.claude-hooks/consultants/<sid>/{summary,transcript,metadata}.*`
and can be re-read via `/consultants show <sid>`.

---

## followup — iterate on a prior consultation

The follow-up **reuses** the parent's per-role message history.
Planner / researcher / critic / synthesizer each pick up where they
left off, so this is materially different from running `ask` again
with a related question — the answer is continuous with the parent.

Use this when the user wants to push back on the synthesizer's
recommendation, drill into a specific point, or ask a related
question that depends on the parent's grounding. For a fresh
question that doesn't depend on prior context, use `ask` instead.

### 1. Resolve the parent sid

Parse the user's args. Look for a leading positional matching
`csl-<YYYY-MM-DD>-<HHMM>-<hex>`; the rest is the follow-up message.

**Explicit sid present:** use it directly. Don't second-guess.

**No sid:** auto-pick the most recent session of any status:

```
claude-consultants list --cwd "$(pwd)" --limit 10
```

Three cases by the newest entry's `status`:

- **`completed`** — use as parent. Tell the user
  "Following up on csl-..., your last completed consultation".
- **`failed`** — chaining off a failed sid is usually CHEAPER than
  chaining off its parent (researcher + critic work survived in
  transcript.db; only the synthesizer flapped). AskUserQuestion:
  (a) **Chain off the failed session (recommended)** — warm reuse;
  (b) **Chain off the failed session's `parent_sid`** — fresh
  research from the last known-good upstream; (c) **Cancel**.
  To resolve the failed session's `parent_sid`, call
  `claude-consultants result <failed_sid>` and read `metadata.parent_sid`.
- **`running`** — previous consultation hasn't finished. Offer to
  wait (poll until complete, then follow up) or cancel. Don't fire
  against a running session.

Empty list → say so plainly; suggest `/consultants <query>` to
start a fresh consultation.

### 2. Optional warmth probe (one quick poll)

```
claude-consultants list-open
```

If the parent sid is in the warm pool, follow-up resolves
sub-second; if not, it auto-reopens from disk (~5–10s). Mention so
a 10s pause doesn't look like a hang. Don't gate on it.

### 3. Fire the follow-up

```
claude-consultants follow-up <parent_sid> --message "<follow-up text>" --cwd "$(pwd)"
```

Optional `--effort ...` to override the tier for this follow-up
(defaults to parent's). Returns:

```json
{"ok": true, "sid": "csl-...-NEW", "parent_sid": "...",
 "status": "running", "status_url": "/v1/consult/csl-..."}
```

### 4. Poll + fetch (same as ask)

`status <new_sid>` until `completed`, then `result <new_sid>`.
Print `summary_markdown` verbatim. Use an extended footer to thread
the lineage:

```
sid: csl-...-NEW · parent: csl-...-OLD · effort: medium ·
duration: 1m 47s · models: planner=... researcher=... critic=...
synthesizer=...
```

Each follow-up is itself a permanent session and can be the parent
of further follow-ups. Chains are intended for iterative refinement
on a single deep topic.

---

## list — show past consultations

```
claude-consultants list --cwd "$(pwd)" [--limit N] [--state STATE]
```

Returns JSON. Format as a compact table or numbered list,
newest-first, including sid, status, effort, duration, and the
question preview (truncated to ~80 chars). Mention that
`/consultants show <sid>` prints any of them in full. Empty list →
say so plainly; suggest `/consultants <query>` to start.

---

## show — print a stored session

Parse the user's args after the `show` verb for one positional sid.

```
claude-consultants show <sid> --cwd "$(pwd)"
```

Reads disk only — no engine call. Print `summary_markdown` verbatim
(front-matter + synthesizer's final answer). Short footer:

```
sid: csl-... · effort: medium · duration: 4m 12s · status: completed
```

If the sid doesn't exist, the CLI returns
`{"ok": false, "error": "no summary for sid ..."}` — surface and
suggest `/consultants list`.

For the full transcript (planner + researcher tool calls + critic
verdict + synthesizer turns), point the user at
`.claude-hooks/consultants/<sid>/transcript.md` — typically too long
for chat. With `--raw`, the CLI dumps the events table from
transcript.db as one JSON object per line.

---

## config — interactive configuration

You drive the underlying `claude-consultants config <subcommand>`
CLI, which validates inputs and atomically writes the TOML file.
Never edit the file directly.

If the user passed sub-args (e.g. `/consultants config set-effort high`),
forward them verbatim:

```
claude-consultants config <subcommand> [args...] --cwd "$(pwd)"
```

Otherwise, walk the interactive dialog:

### 1. Show current state

```
claude-consultants config show --cwd "$(pwd)"
```

Render a status block. Append `, multi-model active` to the Effort
line when `extras_active` is `true`. Show `extra_models` for
researcher + critic (with `(none)` when empty); hide for planner +
synthesizer (engine doesn't fan those out). ctx_max is `auto` when
null, `<N>` when set.

```
=== /consultants config ===
Service mode: smart-start (idle 30 min)   |   always-on
Endpoint: http://127.0.0.1:38096
Effort: xhigh (budget 5, multi-model active)
Topology: council

Roles:
  planner       ENABLED   model=kimi-k2.6:cloud         ctx=auto
  researcher    ENABLED   model=qwen3.5:cloud           ctx=auto
                  extra_models: kimi-k2.6:cloud, deepseek-v4-pro:cloud
  critic        ENABLED   model=deepseek-v4-pro:cloud   ctx=auto
                  extra_models: (none — only used at xmax)
  synthesizer   ENABLED   model=kimi-k2.6:cloud         ctx=auto
                (synthesizer cannot be disabled)
```

### 2. Top-level menu (loop until Done)

AskUserQuestion (single-select, max 4):

1. **Edit a role** — toggle on/off, model, ctx, extras
2. **Change service mode** — always-on or smart-start
3. **Change effort tier** — low/medium/high/max, or x-prefixed
   variants for multi-model fan-out
4. **Done**

Loop back to step 1 after each successful change.

### Subflow A — Edit a role

AskUserQuestion the role (`planner`/`researcher`/`critic`/
`synthesizer`), then the sub-action:

- **Toggle enabled** — only for non-mandatory roles. Reject
  friendlily for `synthesizer`. Warn if disabling planner AND
  researcher (validation error on next consult).
- **Change model** → `claude-consultants config list-models`,
  pick up to 3 candidates + `Other (custom)`, then
  `set-role <role> --model <chosen>`.
- **Manage extra models** — researcher + critic only. Sub-action:
  add / remove / clear / back. Forward to
  `set-role <role> --add-model|--remove-model|--clear-extras ...`.
  On the FIRST add, surface once:
  > Note: extras are only consulted at the x-prefixed effort tiers
  > (xmedium / xhigh / xmax). At low/medium/high/max the primary
  > model runs alone — the extras list is silent.
- **Pin context length** — AskUserQuestion `8192` / `32768` /
  `131072` / `Other`. Then `set-role <role> --ctx <chosen>`.
- **Clear context pin (auto)** — `set-role <role> --ctx auto`.
- **Back**.

### Subflow B — Service mode

AskUserQuestion `always-on` / `smart-start`. Then
`set-service-mode <mode>`. Surface the CLI's `follow_up` string
verbatim (typically: "Run `python install.py` to install/uninstall
the systemd unit, then restart `claude-hooks-daemon`."). This is
NOT something the skill does automatically — unit changes need
user consent.

If smart-start, offer idle timeout: `5min` / `30min (default)` /
`2h` / `Other`. Then `set-idle-timeout <seconds>`.

### Subflow C — Effort tier

Two-round dialog (AskUserQuestion caps at 4 options, 7 tiers
exist). First: family (`base` single-model | `x` multi-model).
Then: pick within family. Show the (N) budget suffix in labels.

- base: `low (1)` / `medium (3)` / `high (5)` / `max (25)`
- x:    `xmedium (3)` / `xhigh (5)` / `xmax (25)` / `back to base`

Then `set-effort <tier>`. When picking an x-tier with empty
researcher `extra_models`, surface:
> Note: x-tiers fan out the researcher across `extra_models`,
> but your researcher has none configured. Behaves like the
> corresponding base tier until you add extras via Subflow A.

Don't block the change.

### After every change

Re-run `config show` and re-render the status block. Return to
step 2's menu unless the user picked Done.

### Per-project overrides

Mention only when the user asks: `set-role ... --project --cwd "$(pwd)"`
edits the per-project TOML (`<project>/.claude-hooks/consultants.toml`)
instead of the user-global (`~/.claude/consultants-config.toml`).
Skill defaults to user-global.

---

## Failure handling (all verbs)

- **CLI returns `{"ok": false, ...}`** → surface the error verbatim.
- **`engine_python not found` / connection refused on `:38096`** →
  the consultants service isn't running. Suggest
  `systemctl --user status claude-hooks-consultants` (always-on
  mode) or `python install.py` if the user hasn't installed yet.
- **Role-specific failure** (e.g. `researcher failed: ...`) →
  surface role + error; suggest `/consultants config` to switch the
  role's model.
- **Cloud upstream flap** → council is particularly retry-prone
  (4 roles × multiple turns); the resilience layer in the proxy
  retries automatically, but exhausted budgets surface here.
  Surface plainly and offer `/consultants followup <failed_sid>`
  to chain off the failed session (researcher + critic work is
  usually salvageable).

## Reference

- **Deep config schema**, role-by-role behavior, effort-tier
  semantics, per-project vs user-global resolution: see
  [`docs/consultants.md`](../../docs/consultants.md).
- **Live engine status & metrics**:
  `claude-consultants config show` (CLI summary) or
  `curl http://127.0.0.1:38096/health` (raw).
- **Past sessions on disk**: `.claude-hooks/consultants/<sid>/`.

## /consultants vs /get-advice

- `/get-advice` is a single-shot conversation with one model. Use
  it when one good model + the project tools can answer the
  question in a single back-and-forth.
- `/consultants` is a multi-agent pipeline. Reach for it when the
  question benefits from a researcher actively grounding in
  project files AND a critic challenging the evidence before the
  final answer.
