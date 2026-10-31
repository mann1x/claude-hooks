# Benchmark — `gemma4-31b-cloud-2026-10-27` (N=3)

Model pin: `gemma4:31b-cloud` (every role). Runs: [`r1`](../gemma4-31b-cloud-2026-10-27-r1/results.md), [`r2`](../gemma4-31b-cloud-2026-10-27-r2/results.md), [`r3`](../gemma4-31b-cloud-2026-10-27-r3/results.md).
Engine HEAD `e9fe819`, subject `bench-baseline-2026-05-07` with the answer key removed, 10:14–11:10 UTC, interleaved with [`glm-5-3-flash-cloud-2026-10-27`](../glm-5-3-flash-cloud-2026-10-27/results.md) 10 min apart, model snapshot verified before r2 and r3. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4.

**First label on the direct cloud route.** Every call went to ollama.com itself (`[cloud] route = "direct"`, `d175b7e`; the bench engine's upstream was ollama.com too), so no host's Ollama served any of it. Bench engine on :38195 with its own HOME, every role pinned, no extra lanes, store off.

| Query | Grade (mode) | Per run | Median wall ± MAD |
|---|---|---|---|
| smoke | **PASS** | PASS / PASS / PASS | 15s ± 0 |
| audit-medium | **A** | A / A / B | 45s ± 0 |
| audit-high | **C** | C / B / C | 46s ± 0 |

Cost per run (list price, Appendix A, no cache discount): $0.036–0.044 (median $0.039).

## Per-query grades

| Run | Query | Grade | Notes |
|---|---|---|---|
| r1 | smoke | **PASS** | One sentence, cited. |
| r1 | audit-medium | **A** | 6/6 — both test sites for the first time; `install.py` named as the fallback, not a gap. |
| r1 | audit-high | **C** | Claims 2–3; no claim 1. Fix: prefix the tombstone with `### LANE_FAILURE` (`council.py:562`) — the tombstone already says the lane failed; addresses neither the status nor the reducer. |
| r2 | smoke | **PASS** | One sentence, cited. |
| r2 | audit-medium | **A** | 5/6 (123, 328, 624, 107, 56); `install.py` omitted. |
| r2 | audit-high | **B** | Claims 2–3; no claim 1. Fix: label failure entries in the synthesizer prompt (`council.py:254`) — actionable, the 10-22 r2 fix again. |
| r3 | smoke | **PASS** | One sentence, cited. |
| r3 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted. |
| r3 | audit-high | **C** | Claims 2–3; no claim 1. Fix: `failed` only on a synthesizer failure (`runner.py:174`) — the status-flip pattern. |

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner | **A** | 5–6 concrete items naming files and functions on every query; its Q3 call was the forced one after the tool turns in all three runs and still returned a plan (thinking off, no leak). |
| researcher | **B** | Cited and batched, two lanes; found both Q2 test sites once in three runs and never states claim 1. |
| critic | **B** | `DECISION: ready` first and cited, but each verdict restates the whole answer, recommendation included, well past five lines. |
| synthesizer | **A** | Bottom line first, short, cited; the citation linter's notes are keyword misses on correct lines (`council.py:245` is `build_synthesizer_messages`). |

**Mix string:** `P:A R:B C:B S:A`

## Verdict

**EVALUATED-ONLY** — Q3 below B (§4).

## Commentary

Unchanged from [2026-10-22](../gemma4-31b-cloud-2026-10-22/results.md) where it counts: Q3 is C with one actionable run (the synthesizer-prompt fix) and claim 1 is never stated; Q2 rises from B to A (r1 found all six sites, r2 five). Still the fastest and cheapest label measured: ~105 s for all three queries at ~$0.04 a run. Against [glm-5.3-flash](../glm-5-3-flash-cloud-2026-10-27/results.md) on the same engine and route: glm is the stronger researcher (claim 1 every run) and the weaker auditor of its own role outputs (reasoning leaked into eight of nine forced answers); both land at C on Q3.
