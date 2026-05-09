# Benchmark — `nemotron-3-super-cloud-2026-05-09` (N=3 aggregate)

Model: `nemotron-3-super:cloud` pinned to every role.

Aggregate of 3 runs against the `bench-baseline-2026-05-07` worktree (commit `83cfd3b`).

- **r1**: `[nemotron-3-super-cloud-2026-05-09-screening/](nemotron-3-super-cloud-2026-05-09-screening/)` (screening; serves as r1 per §5 — same baseline, same engine HEAD, same protocol settings)
- **r2**: `[nemotron-3-super-cloud-2026-05-09-r2/](nemotron-3-super-cloud-2026-05-09-r2/)`
- **r3**: `[nemotron-3-super-cloud-2026-05-09-r3/](nemotron-3-super-cloud-2026-05-09-r3/)`

Engine HEAD across all 3 runs: `6b59116` (v1.1.0). Cloud-snapshot drift: clean across the entire window.
Sweep window: r1 ~04:10–05:23 UTC; r2/r3 ~05:30–06:39 UTC (single 2.5h block).

## Wall + token aggregates per query

Median ± MAD across 3 runs. PT/CT are medians (totals across all roles + lanes per run).

| Query | Wall median ± MAD | Wall range | PT median | CT median | All runs (s) |
|---|---|---|---|---|---|
| smoke        | 121s ± 31 (MAD) | 46–152s | 100271 | 2318 | 121,152,46 |
| audit-medium | 212s ± 60 (MAD) | 106–272s | 156895 | 6928 | 212,272,106 |
| audit-high   | 470s ± 15 (MAD) | 393–485s | 173812 | 14993 | 485,470,393 |

**Total wall:** r1=818s, r2=894s, r3=545s — median 818s, MAD 76s, spread 349s (64%).

## §7 outlier classification

- Wall variance widest of the cohort: r1=818s, r2=894s, r3=546s. Median 818s, MAD 76s, threshold 1198s.
- All three within median ± 2×MAD = [666, 970]? **r3 (546s) is BELOW the lower bound** but §7's OUTLIER rule is asymmetric (`> median + 5×MAD` only — fast runs are not flagged). Treating as CLEAN per literal protocol; flagging the wide variance below.

## Per-query grades (mode across r1/r2/r3)

| Query | Grade | Notes |
|---|---|---|
| smoke | PASS | All 3 runs name 4 roles with `path:line`. |
| audit-medium | A | r1 had path-prefix hallucination (`claude_hooks/scripts/…`); r2/r3 paths clean. Mode across N=3 is A — r1's defect was a one-off cloud sample. |
| audit-high | A | All 3 runs hit 4 claims; recommendation consistently wraps `_wrap_researcher` in `graph.py` with try/except — only model in either 2026-05 sweep whose proposed fix addresses the failure mode it just described. |

## Per-role grades

| Role | Grade | Notes |
|---|---|---|
| planner      | A | concrete items with file targets |
| researcher   | A | good coverage; r1 path-prefix hallucination did not recur on r2/r3 → grade promoted from screening B to N=3 A |
| critic       | A | DECISION line + concise reasoning |
| synthesizer  | A | lead-sentence, evidence-grounded; only Q3 recommendation in cohort that targets the right layer |

Mix string: `P:A R:A C:A S:A`

## Verdict

**PROD-READY** per protocol §4 — all 3 queries `status=completed` across all 3 runs, Q1=PASS, Q2≥B, Q3≥A, no query > 25 min wall, no role hit ≥ 5 retries.

## Commentary

Wall variance widest of the cohort (546–894s, 64% spread). Strongest Q3 hardening recommendation across both 2026-05-07 and 2026-05-09 sweeps — the only model whose proposed fix targets the actual fault layer (wrap researcher_node) rather than shifting the bug elsewhere. The screening r1 path-prefix bug (`claude_hooks/scripts/…`) DID NOT recur on r2/r3 — confirmed one-off cloud sample, not a stable defect. R-grade promoted from B→A on the basis of r2/r3 cleanness.

## Grading caveat — protocol §3 Q3 claim #3

The 2026-05-09 grades use the corrected ground truth: the synthesizer **does** see the failure tombstone via the additive `research` reducer + `build_synthesizer_messages`. See protocol v1.1 changelog at `docs/benchmarks/EVALUATION.md`.
