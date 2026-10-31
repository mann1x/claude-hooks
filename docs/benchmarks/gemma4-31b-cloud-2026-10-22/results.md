# Benchmark — `gemma4-31b-cloud-2026-10-22` (N=3)

Model pin: `gemma4:31b-cloud` (every role). Runs: [`r1`](../gemma4-31b-cloud-2026-10-22-r1/results.md), [`r2`](../gemma4-31b-cloud-2026-10-22-r2/results.md), [`r3`](../gemma4-31b-cloud-2026-10-22-r3/results.md).
Engine HEAD `8f69cee` (engine code identical to `6df99ad`, the minimax runs' HEAD; the commit between touches only `claude_hooks/resources/`), subject `bench-baseline-2026-05-07` with the answer key removed, 10:36–11:02 UTC, runs 10 min apart, model snapshot verified before r2 and r3. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4.

**Why this re-run:** the [2026-09-23 baseline](../gemma4-31b-cloud-2026-09-23/results.md) ran with no reasoning at all — 0 of 43 audit-high calls — because the service silently ignores a thinking level for gemma4 (fixed in `c148363`, which sends `think: true`). Here the planner reasoned on 22 of 24 calls, the synthesizer on 6 of 6, the critic on 6 of 9 and the researcher on 16 of 31.

| Query | Grade (mode) | Per run | Median wall ± MAD |
|---|---|---|---|
| smoke | **PASS** | PASS / PASS / PASS | 16s ± 1 |
| audit-medium | **B** | B / B / B | 30s ± 1 |
| audit-high | **C** | C / B / C | 46s ± 15 |

Cost per run (list price, Appendix A, no cache discount): $0.039–0.042 (median $0.041).

## Per-query grades

| Run | Query | Grade | Notes |
|---|---|---|---|
| r1 | smoke | **PASS** | One sentence, cited. |
| r1 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted; also lists `install.py:2092` (inside the conda-run subprocess script) as a gap. |
| r1 | audit-high | **C** | Claims 2–4; no claim 1. Fix: drop `error` from the tombstone (`council.py:564`) — the status-flip pattern, and it discards the failure record. |
| r2 | smoke | **PASS** | One sentence, cited per role. |
| r2 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted. |
| r2 | audit-high | **B** | Claims 2–4; no claim 1. Fix: warn the synthesizer when `_role_failed == "researcher"` (`council.py:718`) — actionable, addresses the silent gap. |
| r3 | smoke | **PASS** | One sentence, cited. |
| r3 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted. |
| r3 | audit-high | **C** | Claims 2–4; no claim 1. Fix: status `failed` only on a synthesizer failure (`runner.py:172`) — the status-flip pattern. |

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner | **A** | 5–6 concrete items on every Q2 and on r2's Q3; r1 and r3 Q3 returned a "verified these files exist, analysis not done" status report instead of a plan (C) — the engine's forced report after the planner's tool turns, seen in the minimax runs too. |
| researcher | **B** | Cited, but light (2 lanes, 25–30 citations on Q3) and misses both Q2 test sites in every run. |
| critic | **B** | `DECISION: ready` first, cited, catches the lane that claimed a crash (r1, r2 `CORRECTIONS`); r2 and r3 run past five lines. |
| synthesizer | **A** | Bottom line first, short, cited. |

**Mix string:** `P:A R:B C:B S:A`

**Harness caveat (task ch-17):** the two status-report plans (r1, r3 Q3) are the engine's forced cap report — the planner spent its three tool turns on `survey_project` and two `list_files`, asked for a fourth, and the final call asked it for findings instead of a plan. Fixed by `LoopConfig.deliverable`; the critic and synthesizer were not capped in these runs.

## Verdict

**EVALUATED-ONLY** — Q3 below B (§4). On 2026-09-23 (no reasoning) the same label was PROD-READY with Q3 B in all three runs.

## Commentary

Turning reasoning back on did not lift gemma4's grades on this subject. Q1 and Q2 are unchanged (PASS, B — the same four sites, the same omitted test sites), wall is still the fastest measured (~90 s for all three queries) at ~$0.04 a run. Q3 dropped from B to C: with reasoning, two of three runs pick the status-relabel fix that minimax-m3.1-flash picked in all three, where the non-reasoning runs proposed marking the failed lane in the synthesizer's prompt. Claim 1 (`error` / `_role_failed` are last-value channels) is still never stated. Against [minimax-m3.1-flash](../minimax-m3-1-flash-cloud-2026-10-22/results.md) on the same engine: minimax is stronger on evidence (Q2 A, all four Q3 claims) and both land at C on Q3; gemma4 is 3–9× faster per query (median wall) on about half the prompt tokens; minimax-m3.1-flash has no list price to compare cost against.
