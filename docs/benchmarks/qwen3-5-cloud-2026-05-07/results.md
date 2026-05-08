# Benchmark — `qwen3-5-cloud-2026-05-07`

Generated 2026-05-07T10:38:49+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `771bb51`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-3KOY`
Cloud model snapshot: see `models.json`.

Model pin: `qwen3.5:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 106s | 56338 | 9446 | 13 | 9 | `csl-2026-05-07-1030-58a8` |
| audit-medium | medium | completed | 91s | 108799 | 9146 | 17 | 20 | `csl-2026-05-07-1032-1b25` |
| audit-high | high | completed | 303s | 123026 | 13245 | 27 | 45 | `csl-2026-05-07-1033-c508` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 36.1s | 1 | 251 | 3749 | 0 |
| researcher | 54.8s | 11 | 81353 | 2079 | 9 |
| synthesizer | 40.9s | 1 | 863 | 4388 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 28.8s | 1 | 277 | 3155 | 0 |
| researcher | 1.6m | 15 | 159337 | 5603 | 20 |
| synthesizer | 17.3s | 1 | 2089 | 1872 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 34.2s | 1 | 407 | 3404 | 0 |
| researcher | 5.1m | 24 | 483011 | 15749 | 45 |
| critic | 2.0m | 1 | 5247 | 1199 | 0 |
| synthesizer | 15.0s | 1 | 5648 | 997 | 0 |

## Files

Per-query artifacts in this directory:

- **smoke**
  - `smoke.summary.md` — synthesizer answer
  - `smoke.transcript.md` — full role transcript
  - `smoke.waterfall.txt` — per-role wall waterfall
  - `smoke.trace.jsonl` — raw JSONL trace
  - `smoke.metadata.json` — token totals + retries
- **audit-medium**
  - `audit-medium.summary.md` — synthesizer answer
  - `audit-medium.transcript.md` — full role transcript
  - `audit-medium.waterfall.txt` — per-role wall waterfall
  - `audit-medium.trace.jsonl` — raw JSONL trace
  - `audit-medium.metadata.json` — token totals + retries
- **audit-high**
  - `audit-high.summary.md` — synthesizer answer
  - `audit-high.transcript.md` — full role transcript
  - `audit-high.waterfall.txt` — per-role wall waterfall
  - `audit-high.trace.jsonl` — raw JSONL trace
  - `audit-high.metadata.json` — token totals + retries

## Per-query grades (per [`EVALUATION.md`](../EVALUATION.md) §3)

| Query | Grade | Notes |
|---|---|---|
| smoke        | PASS | One sentence; all four roles named with `consultants/config.py:40` citation; no hedging |
| audit-medium | C    | Cites only **2 of 6** ground-truth sites (pgvector.py:123/:328); misses migrate:624, bench_recall:107, and both test sites; **also fabricates** "The referenced commit `4e67dc2` does not exist in the repository history" (it does — same failure mode as the qwen3.5:397b sibling label) |
| audit-high   | A    | All 4 required claims present and cited; claim 1 explicit ("error/_role_failed are plain fields (last-writer-wins)"); recommendation is code-shaped (remove the `error` key from lane-level tombstones at `council.py:563-566`) — note the recommendation's *direction* is debatable (removing the error key would prevent `status=failed` for partial-lane failures, which may be incorrect engine policy), but it's concrete and on a real line so the rubric grade stands |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 7 numbered items with concrete file targets and verification steps ("locate the specific line ... where tool_executor non-string returns or timeouts raise unhandled exceptions"); reasoning chain is structured (item 6 isolates the exception origin, item 7 specifies the fix location) |
| researcher  | B | Real `path:line` citations (council.py:555, graph.py:64-75, runner.py:172-174, agent_loop/runner.py:122); but inserts answer-shaped sub-headers per lane (`## Failure Path Analysis`, `## Recommended Hardening Change`) just like the 397b sibling — drafts the synthesizer's answer rather than producing per-lane evidence; critic flagged inter-round contradiction on status determination |
| critic      | A | Parseable `DECISION: needs_more_research` line; gaps named with explicit `path:line` (`runner.py:172-176`, `graph.py:74-75`, `council.py:564 vs runner.py:174 vs agent_loop/runner.py:122`); flagged contradiction succinctly |
| synthesizer | B | Audit-high A and smoke clean; but audit-medium fabricates the "commit doesn't exist" claim — same synthesizer-quality issue as the 397b sibling; the audit-high recommendation's debatable direction is a softer concern but worth flagging in commentary |

**Mix string:** `P:A R:B C:A S:B`

**Verdict:** EVALUATED-ONLY (Q2 grade C falls below the §4 PROD-READY floor of B)

**Commentary:** qwen3.5:cloud is the cheaper sibling of qwen3.5:397b:cloud and shares the same shape — strong on planner + critic + frontier reasoning (audit-high A), weak on enumeration-heavy audit-medium where it cites only 2 of 6 sites and fabricates a commit-doesn't-exist claim. Wall is mid-pack (smoke 97s, audit-medium 88s, audit-high 300s), faster than kimi everywhere. The audit-high recommendation also has a directional issue worth noting — "remove the error key from tombstones so status doesn't show failed" is the wrong engine policy (the engine *should* mark status=failed when any lane errors), but the rubric grades on path:line + code-shape, both of which it delivers. **Strong critic / planner pick** for cost-conscious heterogeneous mixes; **avoid for synthesizer on audit-shaped queries** where its fabrication tendency would mislead. Direct comparison vs `qwen3-5-397b-cloud-2026-05-07`: same shape, different scale; the smaller model is competitive enough that the larger 397b's premium isn't obviously justified for this work.
