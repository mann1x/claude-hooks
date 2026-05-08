# Benchmark — `qwen3-5-397b-cloud-2026-05-07`

Generated 2026-05-07T10:29:28+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `771bb51`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-NsBk`
Cloud model snapshot: see `models.json`.

Model pin: `qwen3.5:397b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 91s | 61672 | 4725 | 11 | 8 | `csl-2026-05-07-1022-9e36` |
| audit-medium | medium | completed | 91s | 92423 | 8950 | 17 | 23 | `csl-2026-05-07-1023-c9ca` |
| audit-high | high | completed | 242s | 125259 | 13133 | 29 | 47 | `csl-2026-05-07-1025-f8da` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.0m | 1 | 251 | 2816 | 0 |
| researcher | 33.0s | 9 | 82417 | 1637 | 8 |
| synthesizer | 7.7s | 1 | 760 | 896 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 25.1s | 1 | 277 | 2564 | 0 |
| researcher | 1.5m | 15 | 127754 | 5513 | 23 |
| synthesizer | 22.3s | 1 | 2532 | 2517 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 19.1s | 1 | 407 | 2173 | 0 |
| researcher | 5.5m | 26 | 568268 | 19350 | 47 |
| critic | 11.3s | 1 | 5327 | 941 | 0 |
| synthesizer | 33.0s | 1 | 5782 | 1355 | 0 |

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
| smoke        | PASS | One sentence; all four roles named with `consultants/engine/council.py:98-99` citation; no hedging |
| audit-medium | C    | Cites only **2 of 6** ground-truth sites (pgvector.py:123/:328); misses migrate:624, bench_recall:107, and both test sites; **also makes a fabricated claim** ("Commit `4e67dc2` does not exist in this repository") which is false (the commit is in `git log`) — exactly the "fabricates non-existent ... claims" failure mode in the §3 rubric |
| audit-high   | A    | All 4 required claims present and explicitly cited: claim 1 nailed (`error and _role_failed at lines 74-75 are not annotated (plain dict merge, last-write-wins)`); claims 2-3 cited with `path:line`; recommendation is code-shaped (Python diff at `council.py:545-550` adding `error: None` and `_role_failed: None` to successful returns) |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 7 numbered items, each citing concrete file targets with verification steps ("verify how parallel lane exceptions are merged", "identify the exception handling boundary"); item 7 is overly procedural ("Pinpoint the specific line ... where wrapping parallel lane execution would isolate failures") but the rest are sound |
| researcher  | B | Real `path:line` citations on claims (council.py:555-571, graph.py:74-75, runner.py:174); but inserts answer-shaped sub-headers (`## Failure Path Analysis`, `## Verdict`, `## Recommended Hardening Change`) per lane — drafts the synthesizer's answer instead of producing per-lane evidence; critic correctly flagged inter-round contradiction on status determination |
| critic      | A | Parseable `DECISION: needs_more_research` with explicit gap-naming ("show the successful `researcher_node` return dict structure", "confirm exact status logic with surrounding context") and `path:line` for each gap; correctly identified the contradiction across rounds |
| synthesizer | B | Audit-high A and smoke clean; but audit-medium fabricates the "commit doesn't exist" claim — that's a synthesizer-quality issue, not a researcher one (researcher provided the 2 sites; synthesizer chose to lead with a wrong claim about commit existence rather than acknowledge the search didn't surface it) |

**Mix string:** `P:A R:B C:A S:B`

**Verdict:** EVALUATED-ONLY (Q2 grade C falls below the §4 PROD-READY floor of B)

**Commentary:** qwen3.5:397b's frontier-question performance (audit-high A) is the surprise — it explicitly nailed the non-additive last-write-wins behavior with a code-shaped fix, matching the kimi baseline at this difficulty. But the audit-medium C grade is a real foot-gun: only 2 of 6 sites cited and a confidently-wrong claim about commit existence. Strong fit for **planner** (clean numbered plans) and **critic** (substantive gap-naming with `path:line`). Risky for synthesizer in audit / fact-extraction queries where its tendency to fabricate negative claims ("X doesn't exist") would mislead users; safer when the question is reasoning-heavy rather than enumeration-heavy. Wall is fast (audit-high 228s vs kimi 787s), so a P:qwen-397b R:_ C:qwen-397b S:_ mix could work if the researcher and synthesizer roles are filled by labels that ground better.
