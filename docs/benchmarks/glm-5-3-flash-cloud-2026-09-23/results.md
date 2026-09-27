# Benchmark — `glm-5-3-flash-cloud-2026-09-23` (N=3)

Model pin: `glm-5.3-flash:cloud` (every role). Runs: [`r1`](../glm-5-3-flash-cloud-2026-09-23-r1/results.md), [`r2`](../glm-5-3-flash-cloud-2026-09-23-r2/results.md), [`r3`](../glm-5-3-flash-cloud-2026-09-23-r3/results.md).
Engine HEAD `acc1473` (bench engine restarted on it before r1), subject `bench-baseline-2026-05-07` with the answer key removed, 18:16–18:55 UTC, model snapshot verified before r2 and r3. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4.

| Query | Grade (mode) | Median wall ± MAD |
|---|---|---|
| smoke | **PASS** | 15s ± 0 |
| audit-medium | **C** | 76s ± 0 |
| audit-high | **C+** | 107s ± 0 |

Cost per run (list price): $0.050–0.053 (median $0.052).

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner | **A** | 7-item plans with concrete paths/commands on both audits in every run; one smoke planner answered instead of planning. |
| researcher | **A** | Cited and batched (29–34 tools over 9–10 calls on audit-medium), 2–8 linter flags per run. |
| critic | **A** | `DECISION: ready` first and terse in r1/r3; r2 put the decision last after an 11-line verification (B). |
| synthesizer | **A** | Bottom line first, cited, shape matches. |

**Mix string:** `P:A R:A C:A S:A`

**Verdict:** EVALUATED-ONLY (Q2 C, Q3 C+)

**Commentary:** Same engine, same leak-free tree, same UTC window as gemma4: glm-5.3-flash loses on both audits and costs ~25 % more ($0.052 vs $0.041) at ~2.5× the wall. Its Q2 errors are systematic across all three runs — it argues the provider sites are masked and the test sites unreachable — and all three Q3 answers land on the status flip. Its traces are the more complete (it alone names the two-lane `InvalidUpdateError`), so the reasoning is not the problem; the audit verdicts and the recommendation are. Do not replace gemma4 on the council roles.
