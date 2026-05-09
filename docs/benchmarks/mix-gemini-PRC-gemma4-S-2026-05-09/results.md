# Benchmark — `mix-gemini-PRC-gemma4-S-2026-05-09` (N=3 aggregate)

Heterogeneous mix:

- **planner** = `gemini-3-flash-preview:cloud`
- **researcher** = `gemini-3-flash-preview:cloud`
- **critic** = `gemini-3-flash-preview:cloud`
- **synthesizer** = `gemma4:31b-cloud`

(Extras configured for xmedium+/xhigh/xmax fan-out: `researcher` adds
`gemma4:31b-cloud, glm-5.1:cloud`; `critic` adds `glm-5.1:cloud`. They do
NOT fire at the `medium`/`high` effort used for these benches — included
in the per-role config so xmedium+ runs benefit automatically without a
config change.)

Aggregate of 3 runs against `bench-baseline-2026-05-07` worktree (commit `83cfd3b`).

- **r1**: [`mix-gemini-PRC-gemma4-S-2026-05-09-r1/`](mix-gemini-PRC-gemma4-S-2026-05-09-r1/) (originally the `-screening` run, promoted on 2026-05-09)
- **r2**: [`mix-gemini-PRC-gemma4-S-2026-05-09-r2/`](mix-gemini-PRC-gemma4-S-2026-05-09-r2/)
- **r3**: [`mix-gemini-PRC-gemma4-S-2026-05-09-r3/`](mix-gemini-PRC-gemma4-S-2026-05-09-r3/)

Engine HEAD across all 3 runs: `5f7d229` (= `6b59116` v1.1.0 for engine purposes —
the commits between are docs-only, no `consultants/` or `agent_loop/` code touched).
Cloud-snapshot drift across the sweep: clean (gemini-3-flash-preview:cloud
modified 2025-12-17, gemma4:31b-cloud modified 2026-04-02 — both stable).
Sweep window: 2026-05-09 08:09–08:38 UTC (10:09–10:38 local).

## Wall + token aggregates per query

Walls below are `duration_seconds` from `*.metadata.json` (consult duration only);
the bench script's reported wall includes ~5-15s of fixture overhead per query.
Median ± MAD across 3 runs.

| Query | Wall median ± MAD | Wall range | All runs (s) | Prompt tok median | Completion tok median |
|---|---|---|---|---|---|
| smoke        | 22s ± 1 | 21–23s | 23, 21, 22 | 90 k | 2.6 k |
| audit-medium | 57s ± 4 | 34–61s | 57, 61, 34 | 257 k | 8.0 k |
| audit-high   | 127s ± 6 | 120–135s | 120, 127, 135 | 180 k | 18.6 k |

**Total wall:** r1=201s, r2=209s, r3=192s — median 201s ± 9s MAD, spread 17s (8%). Tightest N=3 spread observed across any 2026-05-09 sweep.

## §7 outlier classification

For each query, `median + 5×MAD` is the OUTLIER threshold:

- smoke: 22 + 5 = 27s → all 3 well under, CLEAN
- audit-medium: 57 + 20 = 77s → r1 (57) r2 (61) r3 (34) all under, CLEAN
- audit-high: 127 + 30 = 157s → r1 (120) r2 (127) r3 (135) all under, CLEAN

All 3 runs classified **CLEAN** per §7. No retries on any role across any run
(`retries_by_role: {}` in every metadata file).

## Per-query grades (mode across r1/r2/r3, with v1.2 Q3 sub-rubric)

| Query | Grade | Notes |
|---|---|---|
| smoke | PASS | All 3 runs name the four roles in one sentence with valid `council.py:98(-99)` cites. |
| audit-medium | A | All 3 runs cite the same 6 ground-truth `path:line` import sites with per-site exercisability ruling. r2 added a `pyproject.toml:34` reachability hint. No fabrications. |
| audit-high | **B+** (Q3-bottlenecked) | Q1 + Q2 = A across all 3 runs (graph reducer cites + storage path consistent). Q3 varies — r1+r2 propose defensive `output = str(tool_executor(...))` (B+ — actionable but `len(output)` at runner.py:160 crashes first); r3 proposes synthesizer short-circuit on `state.get("error")` (B — overcorrects, replaces synthesis with error-only message). Q3 median grade is B+. |

## Per-role grades (aggregated across N=3)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 5–8 s wall, clean three-bullet plans across all 9 query-runs; gemini-3-flash-preview's strongest role. |
| researcher  | A | Every audit-medium + audit-high cite valid against the frozen baseline; r3 has the richest cite set (`graph.py:74`, `council.py:562-573`, `storage.py:119-120`, `runner.py:137-154`). No fabrications. |
| critic      | A | Single critic call per audit-high, gemini-3-flash-preview, 18–22 s wall, substantive verdict that flowed into the synthesizer's failure framing across all 3 runs. |
| synthesizer | **A−** | gemma4 produced the cleanest "coherent but degraded answer + status=failed" framing of any 2026-05-09 label (better than gemini's solo runs). Q3 quality varies (B+ / B+ / B) — that's the cap. Knock to A− on the strength of Q1+Q2 and the gemma4-only "explicit degradation acknowledgement" framing that no other label produced. |

**Mix string:** `P:A R:A C:A S:A−`

## Verdict

**PROD-READY** per protocol §4 — all 3 queries `status=completed` across all 3 runs;
Q1=PASS, Q2=A, Q3=B+ (median); no query > 25 min wall; no role hit ≥ 5 retries.

This is the **first confirmed PROD-READY heterogeneous mix** in the benchmark
catalogue.

## Per-role wall medians (per-fire) vs other N=3 cohorts

Median per-fire wall in seconds. Note this mix runs gemini for P/R/C and
gemma4 only for synthesis, so the per-role costs follow each model's
characteristic per-fire wall almost exactly.

| Role | mix (this label) | gemini-3-flash-preview (N=3) | gemma4 (N=3) |
|---|---|---|---|
| planner     | 5.6s   | 5.7s   | 5.9s   |
| researcher  | ~40s   | 39.8s  | 168.0s |
| critic      | 18.6s  | 8.4s   | 18.8s  |
| synthesizer | 22.1s  | 6.3s   | 8.5s   |

Synthesizer wall in this mix (22.1s) is higher than pure gemma4 (8.5s)
because the gemma4 synthesizer in the mix sees a richer `research` list
(produced by the gemini researcher with its tool-heavy retrieval style),
so it has more material to summarise. Net total wall is still the
**fastest A-grade total** of any 2026-05-09 N=3 label (median 201s vs
gemma4's 408s median, gemini's solo at ~145s — but gemini-solo's
synthesizer is only B for the audit-high failure framing).

## Commentary

The mix delivers the documented best-of-both: gemini's per-fire speed on
the dominant `researcher` role (5× faster than pure gemma4) and gemma4's
synthesis quality on the audit-high failure framing (the only model that
explicitly distinguishes "coherent but degraded" from "failed"). Total
wall is 201s median — fastest A-grade label measured at v1.1.0 engine
HEAD.

The Q3 ceiling is real: even with gemma4 doing synthesis, none of the 3
runs of this mix produced the gemma4-pure-r2-style A-grade prompt
augmentation. The mix's synthesizer Q3 quality is **lower** than pure
gemma4 on this specific sub-task — likely because gemma4 here is
synthesising over gemini-style research notes rather than its own.

**Recommended use:**

- **Default for all-roles** on solidPC + pandorum (now applied as the live
  config on both hosts as of 2026-05-09).
- **NOT recommended** as the sole driver for code-diff hardening review
  PRs — for those, prefer pure-gemma4 single-shot consultation, or run
  this mix and gemma4-pure side-by-side and read both Q3 recommendations.

## Grading caveat — protocol §3 Q3 claim #3

Grades use the corrected ground truth per protocol v1.1 (synthesizer **does**
see tombstone via additive `research` reducer) and the v1.2 Q3 correctness
sub-rubric. See [`EVALUATION.md`](../EVALUATION.md) changelog and the
[Q3 actionability audit](../Q3-actionability-audit-2026-05-09.md).
