# Subagent "Warmup" drains subscription tokens (1M+/day)

> **This has been filed as a standalone bug report:
> [anthropics/claude-code#47922](https://github.com/anthropics/claude-code/issues/47922)
> — please track / react there. The comment below is retained for
> context on this broader thread.**

**Claude Code version at time of regression:** ≥ 2.0.77 (reproduced on 2.1.107)
**Symptom first observed:** 2026-04-09 17:12 UTC
**Apparent client-side fix observed:** Claude Code **2.1.121** (see "Update — 2026-04-28" below)
**Related (all closed):** [#17457](https://github.com/anthropics/claude-code/issues/17457) `NOT_PLANNED`, [#16752](https://github.com/anthropics/claude-code/issues/16752), [#16961](https://github.com/anthropics/claude-code/issues/16961), [#25138](https://github.com/anthropics/claude-code/issues/25138)

## Update — 2026-04-28: Warmup traffic disappeared

After upgrading to Claude Code 2.1.121 the literal `"Warmup"`
priming call stopped appearing in the proxy logs entirely.
Observed in `~/.claude/claude-hooks-proxy/*.jsonl` on a single
host that has had `block_warmup: true` continuously enabled:

| Day | Total reqs | Detected Warmups | Blocked | Haiku calls | Sub-100ms calls |
|---|---:|---:|---:|---:|---:|
| 2026-04-26 | 2 953 | 14 | 12 | 32 | 12 |
| 2026-04-27 | 3 379 | **60** | **60** | 96 | 14 |
| 2026-04-28 | 1 304 | **0** | **0** | 3 | 0 |

The fingerprint that was being blocked on 04-27 (`num_messages=1`,
`cc_entrypoint=None`, `model_requested` in `claude-haiku-4-5-*` /
`claude-opus-4-5-*`, `duration_ms < 30 ms` because we stubbed them)
is absent from the 04-28 traffic. The proxy's detector
(`metadata._classify_request`) is unchanged; there is simply
nothing matching the Warmup shape to detect.

This is **a client-side change** — Anthropic's API behaviour did
not move. Claude Code 2.1.121's session bootstrap appears to have
either dropped the Warmup ping or migrated it under the
`sdk-cli` persona, which our classifier (post-`6b23d10`,
2026-04-26) routes as main-conversation rather than priming. We
have not observed an unrecognised single-message ping on 04-28
that could be a renamed Warmup — the count is genuinely zero, not
a coverage gap.

The proxy's `block_warmup: true` is kept on as a safety net for
any future regression. The accompanying statusline indicator
(`· blk=N`) correctly hides itself when the day's count is zero.

## Summary

Starting ~2026-04-09 Claude Code sends a prompt `"Warmup"` as the
first user message to every registered subagent at session start.
Each Warmup is a cold-cache call (no prompt-cache hit), so it pays
1–8 k fresh input tokens per agent per session. On a machine with
plugins that register many agents this alone consumes **99 %+ of
all input tokens** charged against the weekly subscription limit.

## Evidence

Walked every JSONL under `~/.claude/projects/` (27 038 transcripts,
deduped on `message.id + model + requestId` to match
[ccusage](https://github.com/ryoppippi/ccusage)). Script:
[`scripts/weekly_token_usage.py`](https://github.com/mann1x/claude-hooks/blob/main/scripts/weekly_token_usage.py)
(in the unrelated `mann1x/claude-hooks` repo, stdlib only).

1. **First `"Warmup"` user message anywhere:** 2026-04-09 17:12 UTC.
   Before that date — zero Warmups across all transcripts.

2. **Per-day split (Fri 2026-04-10 10:00 CEST → Tue 2026-04-14):**

   | Day | Main input | Sidechain input | Sidechain share |
   |---|---:|---:|---:|
   | Fri | 2 760 | 7 370 | 72.8 % |
   | Sat | 3 795 | 433 308 | 99.1 % |
   | Sun | 5 069 | 346 188 | 98.6 % |
   | Mon | 4 405 | 600 005 | 99.3 % |
   | Tue | 4 493 | 1 304 529 | 99.7 % |

3. **Tuesday breakdown (the worst day):**

   - 1 143 sidechain user messages carried the literal text
     `"Warmup"`.
   - Only 4 sidechain messages carried a real task prompt (a couple
     of deliberate Task-tool invocations).
   - ≈ 99 % of the 1.3 M sidechain input tokens were the Warmup
     spawns, not user work.

4. **Main conversation** consumed only 2–5 k input tokens per day
   thanks to prompt caching. Warmups pay cache-creation costs every
   single session start because each agent's context is new.

## Root cause

Warmup is a new, undocumented pre-spawn step in Claude Code's
session bootstrap. It fires once per registered agent per session
(built-in, user-defined, and plugin agents alike). The user prompt
is hard-coded to `"Warmup"`. No prompt caching reuse between
session starts, so the cost is repeated in full each time.

This is amplified by plugins that register many agents — e.g.
`code-analysis@mag-claude-plugins` registers 13 detective agents.
Users who load several plugins can easily exceed 30 warmups per
session.

Tokens billed against the **subscription weekly limit** — which is
opaque and not published as a number — are drained invisibly.

## Workaround (Tier 1)

Add to `~/.claude/settings.json`:

```json
"env": {
  "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"
}
```

Documented in the Claude Code [Environment variables reference]
(https://code.claude.com/docs/en/env-vars.md) as disabling
*"background task functionality"*; GitHub #17457 confirms it
prevents Warmup spawns. Takes effect the next session.

### Drawbacks / limitations

- **All-or-nothing switch.** Per the official
  [Interactive mode docs](https://code.claude.com/docs/en/interactive-mode.md#background-bash-commands)
  the var *"disables all background task functionality"* —
  collaterally killing:
  - the **Ctrl+B** shortcut (can't background a long-running Bash
    from the TUI)
  - the Bash tool's **`run_in_background: true`** parameter (long
    commands block the whole turn)
  - the **auto-backgrounding** of long-running Bash tools
  - (per #17457) the subagent Warmup spawns
  There is no per-feature toggle. Users who rely on backgrounded
  Bash have to choose between token savings and ergonomics.
- **Applies globally.** Effective in both the interactive TUI and
  `claude -p`. No mode-scoped variant.
- **No per-agent granularity.** Cannot keep Warmup for one "most
  used" agent while disabling it for 12 rarely used ones.
- **No per-plugin granularity.** Cannot leave a plugin's agents
  registered for explicit `Task()` calls but skip their warmups.
- **Built-in agents are unaffected differently.** The separate env
  var `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` only applies in
  non-interactive `-p` mode. In interactive mode the built-ins
  (general-purpose, Explore, Plan, claude-code-guide,
  statusline-setup) always register and — without the main env var
  set — always warm.
- **No visibility.** The reported "X % of weekly limit" shown in
  the UI is computed server-side; nothing under `~/.claude/` exposes
  it programmatically, so users can't see whether Warmup is still
  the dominant consumer after the switch.
- **Undocumented in changelog.** The 2.0.77 release notes do not
  call out Warmup, so users can't correlate their sudden token
  increase with a specific update.

## Requests

1. Document Warmup in the changelog and in
   [Manage costs effectively](https://code.claude.com/docs/en/costs.md).
2. Add a per-agent / per-plugin `warmup: false` key in the agent
   manifest so heavy plugins can opt out without disabling all
   background tasks.
3. Make the reported "weekly limit %" queryable via a CLI flag or
   file so users can audit the effect of changes like
   `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`.
4. Ideally, prompt-cache the Warmup across consecutive session
   starts so it costs only on first use after an agent manifest
   change — a pure performance fix that doesn't require a new
   toggle.

## Dedup validation (anticipating the obvious question)

The per-day totals grow monotonically from Fri → Tue. That pattern
could in principle hide a dedup bug — same messages being counted
more than once — so it was checked three ways before filing.

### 1. Composite key equals single-field key

The script's dedup key is `message.id + model + requestId`. Across
the whole week's sidechain entries, using `message.id` alone
produces the *same* deduped count (4,757). So the composite key
isn't masking any silent collisions — `message.id` is already
unique per API call.

### 2. Raw-to-deduped ratio is consistent at ~2×

Without dedup, the week contains **9 338** sidechain assistant
entries. With dedup, **4 757**. Ratio 1.96×. That matches the
expected replay pattern — Claude Code writes each sidechain turn
into both the originating transcript and the forked-session
transcript it spawns, so most messages show up exactly twice. If
dedup were broken, the per-day deduped input would be ~2× what the
real figures are, *consistently* — but ccusage cross-check below
confirms the deduped numbers are correct.

### 3. Growth tracks session count, not a dedup artefact

| Day | Sidechain sessions | Unique agentIds | Deduped warmup msgs | Deduped input |
|---|---:|---:|---:|---:|
| Fri | **1** | 3 | 95 | 7 370 |
| Sat | 141 | 396 | 567 | 433 308 |
| Sun | 153 | 456 | 696 | 346 188 |
| Mon | 224 | 661 | 1 189 | 600 005 |
| Tue | **390** | 1 148 | 2 210 | 1 304 529 |

Fri has only **1 sidechain session** because Warmup had just
rolled out (first global Warmup: 2026-04-09 17:12 UTC) and the
Fri-10:00-CEST weekly window excludes the handful of Thu-evening
Warmups that preceded it. Most of Fri's other 118 total sessions
predated the feature.

Sessions per day grew Fri→Tue from 1 → 390 as normal work
resumed; warmup-per-session count stayed roughly constant at 2–3
(≈ one per registered agent). The 1.3 M Tue figure decomposes
cleanly as `390 sessions × ~3 agents warmed × ~1–8 k fresh input
tokens each`.

### 4. ccusage cross-check

`ccusage daily -z Europe/Berlin --since 20260410` reports the
same per-day totals within the CET↔CEST window delta. Example:
this script reports `446 100 541` total tokens for Tue; ccusage
reports `446 712 100` (delta ≈ 0.14 %, all attributable to the
fact that ccusage groups by full calendar day while this script
uses the Fri-10:00-CEST reset-shifted day).

So: dedup is legitimate, the growth is real, and it scales with
session count × registered-agent count — exactly as the bug
theory predicts.

## Repro

Using [`mann1x/claude-hooks/scripts/weekly_token_usage.py`](https://github.com/mann1x/claude-hooks/blob/main/scripts/weekly_token_usage.py):

```bash
python3 scripts/weekly_token_usage.py --show-sidechain
# With Warmup active (pre-fix):
#   Sub%Inp column reads 95–99 % on any day with plugins loaded
# After setting CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1:
#   Sub%Inp should drop to single digits within one session start
```
