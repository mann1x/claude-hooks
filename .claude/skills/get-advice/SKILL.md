---
name: get-advice
description: Run a multi-turn LLM-to-LLM conversation with a configured Ollama advisor model to get a focused second opinion on a question, validation request, or technical review. Use when the user invokes /get-advice with a query.
---

# /get-advice — LLM-to-LLM advisor

You are about to run a direct conversation between yourself (Claude
Code) and a second LLM (the "advisor") running on the local Ollama
proxy. The advisor's job is to give a focused second opinion on the
user's query — validate a function, sanity-check a training recipe,
review a design choice, etc.

You orchestrate. The advisor responds. You evaluate quality after
each turn and decide whether to continue the conversation, reset its
context, start a fresh session, or wrap up and report the answer.

## ⚠️ Activation guard — read first

Only execute the workflow below when **all** of these are true:

1. The user's **current turn** explicitly invokes `/get-advice` with
   a query (e.g. `/get-advice check the function foo`,
   `/get-advice validate the training recipe in train.yaml`).
2. The skill is being called for the *first time* in the current user
   request — not an echo from a `<system-reminder>` listing skills
   invoked earlier in this session.

If the trigger is ambiguous, ask one short clarifying question rather
than running silently.

## Step 1 — read settings

Run these and parse the JSON output:

```
claude-advisor get-model
claude-advisor get-effort
claude-advisor get-tools
```

Note: `model`, `ctx_max`, `effort`, `budget_sessions`, `tools`. If
`ctx_max` is null and you anticipate a long conversation, the CLI
will auto-probe via `/api/show` on the first `turn` call.

## Step 2 — craft the first message

The CLI auto-prepends a fixed advisor preamble (LLM-to-LLM framing,
concise/dense replies, tool-grounding policy). Your first message
should add:

- **The user's question** verbatim or lightly tightened
- **The minimum context** the advisor needs to start (1-3 sentences:
  what kind of project this is, what's the current state of the
  thing being asked about, where the relevant files are)
- **What "good" looks like** — e.g. "I'm looking for: a yes/no on
  whether the recipe's LR schedule is reasonable for this dataset
  size, plus one specific concrete change if you'd alter it"

Don't paste large code blocks. The advisor has the project tools
listed in `tools_enabled` and can pull what it needs. Tell it where
to look (`see scripts/train.py:120`) instead of pasting.

## Step 3 — run a turn

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

## Step 4 — evaluate the reply

Decide between four actions:

**(a) Wrap up** — the question is answered, the advisor was decisive,
and you have nothing high-value to push back on. Report to the user.

**(b) Continue same session** — the advisor's reply is partial,
hedging on one specific point, or asks a clarifying question. Send a
follow-up `claude-advisor turn <sid> --message "<reply>"` (no
`--first`).

**(c) Reset** — `reset_recommended: true`. The advisor is about to
hit context. Summarize the conversation so far + carry the open
question:

```
claude-advisor reset <sid> --carryover "<one-paragraph state summary
+ the specific gap left to resolve>"
```

The CLI returns a `new_sid`; immediately call
`claude-advisor turn <new_sid> --first --message "<your carryover>"`.
Forced resets do **not** count toward the effort budget.

**(d) Start a new session deliberately** — quality is poor in a way
context-reset won't fix (advisor confused, repeating itself,
contradicting earlier turns), and you want a fresh angle. Build a
new `--first` turn with a refined question. This **does** count
toward `budget_sessions`.

### Quality signals to watch for

- **Hedging without commitment**: "could be either", "depends",
  "you might consider X or Y" — push for a pick + reasoning.
- **Repetition**: the new turn restates the previous reply with
  cosmetic changes — likely the advisor is out of ideas; reset or
  end.
- **Self-contradiction**: turn N says A, turn N+2 says ¬A — fresh
  session needed.
- **Tool spinning**: many tool calls but no synthesis — try a
  pointier question or drop tools (`set-tools none`).
- **Question dodging**: the advisor answers an adjacent question
  but not the one asked — reframe or push.

If the advisor is good (decisive, grounded, useful), say so once
internally and move toward wrap-up — don't keep prodding for the
sake of using the budget.

## Step 5 — track sessions vs effort budget

Maintain two counters in your head for this invocation:
- `sessions_used` — incremented when you start a fresh session
  (Step 4 option d), starts at 1 for the initial session
- `forced_resets` — incremented on Step 4 option c (informational,
  NOT counted against budget)

Stop when **any** of:
- The question is answered AND quality is fine.
- `sessions_used >= budget_sessions`.
- Two consecutive sessions have produced incoherent or non-progress
  responses.

## Step 6 — report to the user

Final message includes:
- The advisor's bottom-line answer (one paragraph, your synthesis)
- Concrete recommendations or actions, if any
- Footer: `model: <name> · sessions: N · resets: M · ctx_used: X%`

If the advisor was unable to answer (small ctx + complex query, or
endpoint down), say so plainly — don't paper over.

## Failure handling

- CLI returns `{"ok": false, "error": "..."}`: surface the error,
  recommend `/get-advice--model` to switch model or check
  connectivity to `192.168.178.2:11433`.
- Advisor reply empty: try once more with a sharper question;
  treat second empty reply as session failure.
- Network timeout (600s): treat as session failure, do not retry
  silently.

## Notes

- The session ID is yours to choose; reuse the same one only within
  one /get-advice invocation. Different invocations should use
  different sids so cleanup works correctly.
- `claude-advisor cleanup` runs automatically on a 24h window — no
  need to call it manually.
- Keep the budget honest: `low` (1 session) means one shot. Don't
  silently inflate to `medium` if the answer is unsatisfying.
