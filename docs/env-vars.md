# Claude Code env vars — a curated reference

Compiled from [anthropics/claude-code#42796](https://github.com/anthropics/claude-code/issues/42796)
and official docs, filtered to the ones most useful for subscription
token control and for sidestepping regressions. Every entry includes
the **concrete effect**, the **source** where it was recommended, and
— where applicable — **our honest field verdict** from running it on
this project.

All of these live in `~/.claude/settings.json` under the top-level
`"env"` object:

```json
{
  "env": {
    "NAME": "value",
    ...
  }
}
```

---

## Token-saving / rate-limit

### `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`

**Effect (official docs):** disables *all* background-task functionality:
- Ctrl+B shortcut (TUI backgrounding)
- Bash tool's `run_in_background: true` parameter
- Auto-backgrounding of long-running Bash tools
- **Subagent Warmup spawns at session start** (per `#17457`,
  confirmed empirically in [issue #47922](https://github.com/anthropics/claude-code/issues/47922))

**Why it matters.** Warmup is the single biggest silent token drain
on subscription users — on a machine with plugins that register
several subagents it consumed 99 %+ of daily input tokens on our
hosts. See the analysis at
[`docs/issue-warmup-token-drain.md`](./issue-warmup-token-drain.md).

**Verdict:** ✅ **Recommended** as first mitigation if your weekly
limit is hitting early. All-or-nothing though — if you rely on
Ctrl+B / `run_in_background` you lose them too.

### `CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000`

**Effect:** force a shorter effective context window — autocompact
kicks in once 400 k tokens are used, instead of letting the context
fill to 1 M.

**Source:** [@bcherny, Anthropic staff](https://github.com/anthropics/claude-code/issues/42796#issuecomment-4214556090)
recommended it as part of the post-regression triage stack.

**Verdict:** ⚠️ **Situational.** If you notice degraded reasoning
once you cross 400 k context, this keeps you in the sweet spot.
Didn't change token usage materially for us — we already compact
manually.

### `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=75`

**Effect:** autocompact at 75 % window fill instead of the default
(higher) threshold.

**Source:** [@Shramkoweb](https://github.com/anthropics/claude-code/issues/42796)
posted the full stack; not documented by Anthropic.

**Verdict:** ⚠️ **Aggressive.** Compacts earlier, losing more
mid-session state. Worth trying if the model degrades late in long
sessions.

---

## Thinking control

### `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`

**Effect:** disables the adaptive-thinking feature that auto-adjusts
reasoning budget per turn; forces a fixed budget determined by
`MAX_THINKING_TOKENS` (or the effort level).

**Source:** [@bcherny, Anthropic staff](https://github.com/anthropics/claude-code/issues/42796):
*"we're investigating with the model team. interim workaround:
`CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1` forces a fixed reasoning
budget instead of letting the model decide per-turn."* 37 mentions
in the thread.

**Verdict:** ❌ **Not recommended on this project.** Tried it for
several days — it introduced a noticeable quality degradation,
more trivial mistakes, and more back-and-forth. With claude-hooks
in place we did not need this workaround; your workflow may
differ.

### `MAX_THINKING_TOKENS=63999`

**Effect:** caps thinking at 63 999 tokens per turn.

**Source:** @Shramkoweb, @sqdshguy — paired with
`DISABLE_ADAPTIVE_THINKING` in the recommended stack.

**Verdict:** ❌ **Not recommended on this project** when combined
with the adaptive-thinking disable. Skip it if you're skipping the
adaptive-thinking switch.

### `CLAUDE_CODE_EFFORT_LEVEL=max`

**Effect:** sets the session's effort knob to maximum.

**Source:** @qrdlgit.

**Verdict:** ⚠️ **Untested on this project.** Same class of knob as
the `/effort` slash command.

---

## Model pinning

### `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-5-20251101`

**Effect:** forces Opus calls to use 4.5 instead of 4.6.

**Source:** [@WeZZard](https://github.com/anthropics/claude-code/issues/42796)
for users who feel Opus 4.6 regressed.

**Verdict:** ❌ **Not recommended on this project.** 4.6 has worked
well for us since the regression debate; reverting loses 1 M-context
support.

### `CLAUDE_CODE_DISABLE_1M_CONTEXT=1`

**Effect:** caps Opus context at 200 k (the pre-1 M default).
Motivation: 1 M context is "extra usage" on Max plans.

**Source:** @WeZZard.

**Verdict:** ❌ **Not recommended on this project.** We rely on 1 M
context for this codebase. If your subscription cost is the
overriding concern, this is a big lever.

---

## Safety valves

### `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`

**Effect:** disables experimental beta flags at the API level.
@xdevs23 warns *"the naming is misleading — it disables more than
you actually want"*.

**Verdict:** ⚠️ Try only after reading @xdevs23's patched-fork
notes.

### `CLAUDE_CODE_SIMPLE=1` (aka `claude --bare`)

**Effect:** disables MCPs, CLAUDE.md auto-discovery, hooks, plugin
sync, auto-memory, keychain reads, background prefetches. Useful
for triaging whether a customization is the problem.

**Source:** documented by Anthropic.

**Verdict:** ⚠️ **Diagnostic only** — if you set this you lose
claude-hooks itself. Do not leave on.

### `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1`

**Effect:** in `claude -p` non-interactive mode only, disables
built-in agents (general-purpose, Explore, Plan,
claude-code-guide, statusline-setup). Does **not** apply in the
interactive TUI.

**Verdict:** ⚠️ **Scripted jobs only.** Useful when wrapping
`claude -p` in shell scripts where you don't need the built-ins.

---

## The "bcherny stack" in full

For reference, the recommended workaround bundle posted by
[@bcherny](https://github.com/anthropics/claude-code/issues/42796):

```json
{
  "model": "opus",
  "env": {
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
    "MAX_THINKING_TOKENS": "63999",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"
  }
}
```

**Our honest verdict on this project:** the stack introduced more
trivial mistakes and slower iteration than it saved. With
claude-hooks-backed Qdrant + Memory KG recall the regression the
stack targets never materialised for us. YMMV; we leave these
documented here so others can try without having to dig through
420 comments on issue #42796.

Forks / alternatives worth knowing:
- [xdevs23/claude-code-10x](https://codeberg.org/xdevs23/claude-code-10x)
  — patched CC with some features disabled and 200 k Opus 4.6
  restored via one env var.
- Reverting to an older `@anthropic-ai/claude-code` npm version —
  tested on this project, no meaningful improvement.
