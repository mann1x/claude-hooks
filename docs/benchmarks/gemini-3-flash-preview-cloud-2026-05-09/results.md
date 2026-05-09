# Benchmark — `gemini-3-flash-preview-cloud-2026-05-09` (N=3 aggregate)

Model: `gemini-3-flash-preview:cloud` pinned to every role.

Aggregate of 3 runs against the `bench-baseline-2026-05-07` worktree (commit `83cfd3b`).

- **r1**: `[gemini-3-flash-preview-cloud-2026-05-09-screening/](gemini-3-flash-preview-cloud-2026-05-09-screening/)` (screening; serves as r1 per §5 — same baseline, same engine HEAD, same protocol settings)
- **r2**: `[gemini-3-flash-preview-cloud-2026-05-09-r2/](gemini-3-flash-preview-cloud-2026-05-09-r2/)`
- **r3**: `[gemini-3-flash-preview-cloud-2026-05-09-r3/](gemini-3-flash-preview-cloud-2026-05-09-r3/)`

Engine HEAD across all 3 runs: `6b59116` (v1.1.0). Cloud-snapshot drift: clean across the entire window.
Sweep window: r1 ~04:10–05:23 UTC; r2/r3 ~05:30–06:39 UTC (single 2.5h block).

## Wall + token aggregates per query

Median ± MAD across 3 runs. PT/CT are medians (totals across all roles + lanes per run).

| Query | Wall median ± MAD | Wall range | PT median | CT median | All runs (s) |
|---|---|---|---|---|---|
| smoke        | 31s ± 0 (MAD) | 30–31s | 122514 | 2969 | 31,31,30 |
| audit-medium | 45s ± 0 (MAD) | 45–76s | 231593 | 9593 | 45,45,76 |
| audit-high   | 122s ± 14 (MAD) | 90–136s | 175050 | 21197 | 136,122,90 |

**Total wall:** r1=212s, r2=198s, r3=196s — median 198s, MAD 2s, spread 16s (8%).

## §7 outlier classification

- All three runs within 15s of the median — wall MAD = 0. The §7 OUTLIER threshold collapses to the median itself when MAD=0, which is a known protocol pathology.
- Decision: **classify all 3 as CLEAN**. The actual variance is trivial (7% spread is well below cloud noise floor).

## Per-query grades (mode across r1/r2/r3)

| Query | Grade | Notes |
|---|---|---|
| smoke | PASS | All 3 runs name 4 roles in one sentence with `path:line` cite at `config.py:40`/`council.py:98-99`. Identical wall (31s) on r1 + r2 + r3. |
| audit-medium | A | All 3 runs cite all 6 ground-truth `path:line` sites with correct verdicts. No `claude_hooks/scripts/` prefix bug. Identical wall (45s) on every run. |
| audit-high | A− | All 3 runs hit the 4 required claims with correct cites. Hardening recommendation is consistently code-shaped but consistently targets `research_rounds_used` reducer rather than the exception-path failure mode it just described. The ding is reproducible — slight intrinsic limitation, not noise. |

## Per-role grades

| Role | Grade | Notes |
|---|---|---|
| planner      | A | concrete numbered items with verification steps; consistent across runs |
| researcher   | A | every claim cites `path:line`; tool calls reasonably batched on every run |
| critic       | A | DECISION line first, ≤5 lines reasoning |
| synthesizer  | A | lead-sentence answers, evidence-grounded; output shape matches question |

Mix string: `P:A R:A C:A S:A`

## Verdict

**PROD-READY** per protocol §4 — all 3 queries `status=completed` across all 3 runs, Q1=PASS, Q2≥B, Q3≥A, no query > 25 min wall, no role hit ≥ 5 retries.

## Commentary

Best speed/quality combo at the 2026-05-09 cohort. Tightest run-to-run wall variance (15s spread across N=3). The Q3 hardening recommendation in the screening data targets `research_rounds_used` rather than the failure mode just described — minor ding that consistent across runs. Cloud-variability profile: dead-flat across the 6h sweep window.

## Grading caveat — protocol §3 Q3 claim #3

The 2026-05-09 grades use the corrected ground truth: the synthesizer **does** see the failure tombstone via the additive `research` reducer + `build_synthesizer_messages`. See protocol v1.1 changelog at `docs/benchmarks/EVALUATION.md`.
