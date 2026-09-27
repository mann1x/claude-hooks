# Benchmark — `gemma4-31b-cloud-2026-09-23` (N=3)

Model pin: `gemma4:31b-cloud` (every role). Runs: [`r1`](../gemma4-31b-cloud-2026-09-23-r1/results.md), [`r2`](../gemma4-31b-cloud-2026-09-23-r2/results.md), [`r3`](../gemma4-31b-cloud-2026-09-23-r3/results.md).
Engine HEAD `acc1473` (bench engine restarted on it before r1), subject `bench-baseline-2026-05-07` with the answer key removed, 18:16–18:55 UTC, model snapshot verified before r2 and r3. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4.

| Query | Grade (mode) | Median wall ± MAD |
|---|---|---|
| smoke | **PASS** | 15s ± 0 |
| audit-medium | **B** | 30s ± 1 |
| audit-high | **B** | 30s ± 1 |

Cost per run (list price): $0.039–0.045 (median $0.040).

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner | **A** | 5–6 item plans with concrete targets on both audits; on smoke the planner answered directly in all runs. |
| researcher | **B** | Cited, but light (11–19 tools over 5–6 calls) and it misses two of the six Q2 sites in every run. |
| critic | **A** | `DECISION: ready` first, terse and cited (r1 verbose at 8 lines). |
| synthesizer | **A** | Bottom line first, cited; every Q3 recommendation is the actionable failure-aware-synthesizer shape. |

**Mix string:** `P:A R:B C:A S:A`

**Verdict:** PROD-READY

**Commentary:** Holds its place as the council default on today's engine: PASS / B / B across three runs, the cheapest label measured ($0.040) and the fastest (~75 s for all three queries). It is down from its May PASS / A / A: on the leak-free tree it misses two Q2 sites every run and never states claim 1 on Q3. Whether May's A grades owed anything to the answer key being reachable, or the 3 → 2 lane cap narrowed its research, this data cannot separate; both changed at once.
