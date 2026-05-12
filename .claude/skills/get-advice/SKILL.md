---
name: get-advice
description: LLM-to-LLM advisor — runs a multi-turn conversation with a configured Ollama model to get a focused second opinion on a question, validation request, or technical review. Default verb is `ask <question>` (also implicit — `/get-advice <question>` works). Subcommands — `ask` runs / continues a session; `model [NAME [CTX]]` reports or sets the advisor model and context length; `effort [tier]` reports or sets the sessions-per-invocation budget (`low`/`medium`/`high`/`max`); `tools [spec]` reports or sets the tool list exposed to the advisor (CSV / `all` / `none`). For multi-agent cross-cutting questions use /consultants instead.
---

# /get-advice — LLM-to-LLM advisor dispatcher

You are the dispatcher for an advisor LLM (the "advisor") running
on the local Ollama proxy. The advisor's job is to give a focused
second opinion — validate a function, sanity-check a training
recipe, review a design choice. You orchestrate; the advisor
responds; you evaluate quality and decide whether to continue,
reset, or wrap up.

## ⚠️ Activation guard — read first

Only execute when **both** are true:

1. The user's **current** turn explicitly invokes `/get-advice`.
2. This is the *first* call in the current user request — not an
   echo from a `<system-reminder>` listing skills invoked earlier.

If the trigger is ambiguous, ask one short clarifying question
rather than running silently.

## Verb dispatch

Parse the first whitespace-separated token of the user's args
after `/get-advice`:

| First token  | Verb     | Action                                                    |
|--------------|----------|-----------------------------------------------------------|
| `ask`        | ask      | Drop the verb; treat the rest as the question.            |
| `model`      | model    | Drop the verb; report (no args) or set the advisor model. |
| `effort`     | effort   | Drop the verb; report (no args) or set the effort tier.   |
| `tools`      | tools    | Drop the verb; report (no args) or set the tool spec.     |
| anything else (incl. empty) | ask | Implicit ask: treat the **entire** arg as the question. |

Examples:

- `/get-advice validate this regex` → implicit **ask**
- `/get-advice ask check the function foo` → explicit **ask**
- `/get-advice model` → **model** (report)
- `/get-advice model kimi-k2.6:cloud 131072` → **model** (set name + ctx)
- `/get-advice effort high` → **effort** (set)
- `/get-advice tools read_file,grep,glob` → **tools** (set CSV)
- `/get-advice tools none` → **tools** (clear)

---

## ask — run / continue an advisor conversation

### 1. Read settings

```
claude-advisor get-model
claude-advisor get-effort
claude-advisor get-tools
```

Parse each JSON output and note: `model`, `ctx_max`,
`effort`, `budget_sessions`, `tools`. If `ctx_max` is null and you
anticipate a long conversation, the CLI auto-probes via
`/api/show` on the first `turn` call.

### 2. Craft the first message

The CLI auto-prepends a fixed advisor preamble (LLM-to-LLM framing,
concise/dense replies, tool-grounding policy). Your first message
adds:

- **The user's question** verbatim or lightly tightened.
- **Minimum context** (1–3 sentences: what kind of project this
  is, current state of the area in question, where relevant files
  live).
- **What "good" looks like** — e.g. "yes/no on whether the LR
  schedule is reasonable for this dataset size, plus one specific
  concrete change if you'd alter it".

Don't paste large code blocks. The advisor has the tools listed in
`tools_enabled` and can pull what it needs — tell it where to look
(`see scripts/train.py:120`) instead of pasting.

### 3. Run a turn

Use a stable session id like `advice-<short-tag>-<timestamp>`:

```
claude-advisor turn <sid> --first --message "<your message>" --cwd "$(pwd)"
```

Parse the JSON response:

- `reply` — the advisor's text
- `usage.prompt_eval_count` / `eval_count` — most recent turn
- `ctx_used_pct` — last prompt as fraction of `ctx_max` (null if
  `ctx_max` unknown)
- `reset_recommended` — true when ctx threshold hit
- `turns` — turn count in current session

### 4. Evaluate the reply

Four actions:

**(a) Wrap up** — question answered, advisor decisive, nothing
high-value to push back on. Report to the user.

**(b) Continue same session** — reply is partial, hedging on one
specific point, or asks a clarifying question. Send a follow-up
`claude-advisor turn <sid> --message "<reply>"` (no `--first`).

**(c) Forced reset** — `reset_recommended: true`; advisor is about
to hit context. Summarize state + carry the open question:

```
claude-advisor reset <sid> --carryover "<one-paragraph state summary + the specific gap left to resolve>"
```

CLI returns a `new_sid`; immediately call
`claude-advisor turn <new_sid> --first --message "<your carryover>"`.
Forced resets do **not** count toward the effort budget.

**(d) Start a new session deliberately** — quality is poor in a
way context-reset won't fix (confused, repeating, contradicting
earlier turns), and you want a fresh angle. New `--first` turn
with a refined question. This **does** count toward
`budget_sessions`.

### Quality signals to watch for

- **Hedging without commitment** ("could be either", "depends",
  "X or Y") — push for a pick + reasoning.
- **Repetition** — new turn restates previous reply cosmetically;
  advisor out of ideas; reset or end.
- **Self-contradiction** (turn N says A, turn N+2 says ¬A) →
  fresh session.
- **Tool spinning** — many tool calls, no synthesis. Try a pointier
  question or drop tools (`/get-advice tools none`).
- **Question dodging** — advisor answers an adjacent question; reframe.

If the advisor is good (decisive, grounded, useful), say so once
internally and move toward wrap-up — don't keep prodding for the
sake of using the budget.

### 5. Track sessions vs effort budget

Two counters in your head for this invocation:

- `sessions_used` — incremented when you start a fresh session
  (option d); starts at 1 for the initial session.
- `forced_resets` — incremented on option c (informational, NOT
  counted against budget).

Stop when **any** of:

- The question is answered AND quality is fine.
- `sessions_used >= budget_sessions`.
- Two consecutive sessions have produced incoherent / non-progress
  responses.

### 6. Report to the user

Final message:

- Advisor's bottom-line answer (one paragraph, your synthesis).
- Concrete recommendations or actions, if any.
- Footer: `model: <name> · sessions: N · resets: M · ctx_used: X%`.

If the advisor was unable to answer (small ctx + complex query, or
endpoint down), say so plainly — don't paper over.

### Session-id discipline

The session id is yours to choose; reuse the same one only within
one `/get-advice` invocation. Different invocations should use
different sids so the auto-cleanup window (24 h) works correctly.
`claude-advisor cleanup` runs automatically — no manual call.

---

## model — report or set the advisor model

**No args** → report:

```
claude-advisor get-model
```

Surface `model`, `ctx_max` (or "auto-detect" if null), and the
`ctx_max_explicit` flag. Add a one-line reminder that
`/get-advice model NAME [CTX]` changes it.

**One arg (name)** → set model, leave ctx auto:

```
claude-advisor set-model <name>
```

**Two args (name + ctx)** → set both:

```
claude-advisor set-model <name> --ctx <integer>
```

Valid ctx values are positive integers (tokens). The CLI validates
and surfaces errors verbatim; surface them to the user as-is.

---

## effort — report or set the effort tier

**No args** → report:

```
claude-advisor get-effort
```

Surface `effort` tier, matching `budget_sessions`, and the
`available` field from the output.

**One arg** → set:

```
claude-advisor set-effort <tier>
```

Valid tiers: `low`, `medium`, `high`, `max`. CLI rejects anything
else with an error you should surface verbatim.

### Effort tiers

| Tier   | Sessions | Use when                                                |
|--------|----------|---------------------------------------------------------|
| low    | 1        | Quick sanity check; trust the first answer or none.     |
| medium | 3        | Default — one initial + up to two refinements / angles. |
| high   | 5        | Hard question; want the advisor to push deeper.         |
| max    | 25       | Genuine deep dive; advisor gets long runway.            |

Forced resets due to context-window saturation do **not** count
against this budget — only deliberate fresh sessions (option d
above).

The setting is global (not per-project) and persists at
`~/.claude/get-advice-config.json`. Changes take effect on the next
`/get-advice` invocation.

---

## tools — report or set exposed tools

**No args** → report:

```
claude-advisor get-tools
```

Surface the current `tools` list and the full `known` set.

**One arg (CSV / `all` / `none`)** → set:

```
claude-advisor set-tools "<spec>"
```

Special tokens:

- `all` → enable every known tool.
- `none` → enable none (advisor runs in pure-chat mode; the
  "tools available" addendum in the grounding system prompt is
  suppressed, so the advisor isn't told it has tools it can't call).
- Otherwise: comma-separated list of tool names. Unknown names are
  rejected with an error listing valid names; surface verbatim.

### Known tools and when to drop them

| Tool             | Token cost                | When to drop for small-ctx advisor                                                                 |
|------------------|---------------------------|----------------------------------------------------------------------------------------------------|
| `read_file`      | low                       | Almost never — keep for any code review.                                                           |
| `grep`           | low-med                   | Keep for any "find / search" question.                                                             |
| `glob`           | low                       | Keep — cheap, occasionally useful.                                                                 |
| `list_files`     | low                       | Keep — cheap orientation tool.                                                                     |
| `survey_project` | **high** (~2 KB)          | **Drop first** for small-ctx advisor. First call alone fills 10–20% of an 8k-ctx window. Useful only for "give me the lay of the land" queries. |
| `recall_memory`  | medium-high (depends)     | Drop if recall returns large chunks or the advisor has no project history to mine.                 |

Default is all six; tighten when the advisor has a small context
window or when you've observed it spinning on tools without
synthesizing.

Setting takes effect on the next `/get-advice` invocation. Tools
are intersected with the runner's known specs at call time — if a
saved name is later removed from the runner, it silently drops out
(no error).

---

## Failure handling (all verbs)

- **CLI returns `{"ok": false, "error": "..."}`** → surface the
  error. For `ask`, recommend `/get-advice model` to switch model
  or check connectivity to the proxy (default `192.168.178.2:11433`).
- **Advisor reply empty** → try once more with a sharper question;
  treat a second empty reply as session failure.
- **Network timeout (600s)** → session failure, do not retry silently.

## Reference

- Deep config behavior, persistence path, host-vs-LAN proxy
  routing: see [`docs/get-advice.md`](../../docs/get-advice.md).
- Live state: `claude-advisor get-model` / `get-effort` /
  `get-tools` (CLI). The on-disk config is
  `~/.claude/get-advice-config.json`.

## /get-advice vs /consultants

- `/get-advice` is a single-shot conversation with one model.
- `/consultants` is a multi-agent pipeline (planner / researcher /
  critic / synthesizer). Reach for it when the question benefits
  from a researcher actively grounding in project files AND a
  critic challenging the evidence before the final answer.
