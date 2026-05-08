# Benchmark — `minimax-m2-7-cloud-2026-05-07`

Generated 2026-05-07T10:21:22+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `771bb51`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-NhRc`
Cloud model snapshot: see `models.json`.

Model pin: `minimax-m2.7:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 46s | 84450 | 734 | 13 | 13 | `csl-2026-05-07-1006-b8a8` |
| audit-medium | medium | completed | 106s | 95254 | 3384 | 14 | 32 | `csl-2026-05-07-1006-bbd1` |
| audit-high | high | completed | 757s | 244789 | 22382 | 25 | 39 | `csl-2026-05-07-1008-4409` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 3.9s | 1 | 282 | 177 | 0 |
| researcher | 49.9s | 11 | 241814 | 1150 | 13 |
| synthesizer | 5.8s | 1 | 772 | 140 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 16.1s | 1 | 270 | 422 | 0 |
| researcher | 2.5m | 12 | 311518 | 5003 | 32 |
| synthesizer | 14.2s | 1 | 2275 | 567 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 6.3m | 1 | 472 | 13856 | 0 |
| researcher | 8.5m | 22 | 1047072 | 16374 | 39 |
| critic | 29.0s | 1 | 22018 | 951 | 0 |
| synthesizer | 40.5s | 1 | 22692 | 1074 | 0 |

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
| smoke        | PASS | One sentence (compound, slightly verbose for smoke); all four roles named with role-purpose summary; cites `consultants/engine/council.py:116,126,144,156`; no hedging |
| audit-medium | A    | All 6 ground-truth sites cited correctly; install.py protected pattern explicitly identified (`install.py:2127`, `2524`, `2647`); test sites correctly flagged as "appropriate as-is" / "silent no-op acceptable" |
| audit-high   | C    | Synthesizer **opens with a wrong assertion** ("The specific files (...) **do not exist at those paths** in this repository") that contradicts the rest of its own answer; the trace that follows is structurally correct (claim 2 ✓, claim 3 ✓, claim 4 ✓ with code block) but the lead hedge undermines the answer's reliability — a synthesizer that hedges its own evidence is a bigger problem than missing one claim |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | C | 7 numbered items but several are vague ("Trace one-lane failure through all four stages", "Determine if council produces answer or surfaces `status=failed`" — the latter just restates the question); the planner output also mixes inline tool-call placeholders within the plan, which is wrong shape (planner should plan, not start executing) |
| researcher  | C | Researcher inserts its own `## Failure Path Analysis`, `## ONE hardening recommendation`, etc. sub-headers — drafting the synthesizer's answer rather than producing per-lane evidence reports; the critic explicitly flagged that "filesystem search returned zero matches for `consultants/` as a directory" — citations to unread files; tool-call discipline is the central failure here |
| critic      | A | Parseable `DECISION: needs_more_research` line; gaps named with explicit `path:line` ("`consultants/engine/council.py` does not exist", "Round 2 says exception propagates ... Rounds 1 and 3 say the opposite"); requested specific verification snippets; the critic's diagnosis was correct (the synthesizer LATER walked into exactly the "files don't exist" hedge the critic was trying to resolve before synthesis) |
| synthesizer | B | Two clean queries (smoke + audit-medium A) but audit-high opens with a wrong claim then hedges; the structural trace is correct but the lead damages user trust — would benefit from synthesizer-side guard ("when researcher disagrees about basic facts, ask for verification rather than synthesizing") |

**Mix string:** `P:C R:C C:A S:B`

**Verdict:** EVALUATED-ONLY (Q3 grade C falls below the §4 PROD-READY floor of B)

**Commentary:** minimax-m2.7's standout role is **critic** — its needs_more_research verdict on audit-high was the most diagnostically useful of any label, naming the exact contradictions across rounds and requesting the specific snippet ranges that would resolve them. The planner and researcher roles are weaker: planner mixes meta-procedural items with concrete ones, and researcher writes synthesizer-shaped reports rather than per-lane evidence (with citations to files it hadn't actually opened, per the critic's flag). For a heterogeneous mix this model is a strong **critic-only** pick, especially as one extra in `xmax` critic fan-out where its tendency to flag inter-round contradictions complements gemma4 / glm-5.1's more affirmative styles. Not recommended for planner / researcher / synthesizer at frontier-question difficulty until the answer-shape discipline improves. Wall times are middling (smoke 36s, audit-high 752s).
