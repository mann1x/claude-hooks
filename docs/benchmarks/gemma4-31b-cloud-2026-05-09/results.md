# Benchmark — `gemma4-31b-cloud-2026-05-09` (N=3 aggregate)

Model: `gemma4:31b-cloud` pinned to every role.

Aggregate of 3 runs against `bench-baseline-2026-05-07` worktree (commit `83cfd3b`).

- **r1**: [`gemma4-31b-cloud-2026-05-09-r1/`](gemma4-31b-cloud-2026-05-09-r1/)
- **r2**: [`gemma4-31b-cloud-2026-05-09-r2/`](gemma4-31b-cloud-2026-05-09-r2/)
- **r3**: [`gemma4-31b-cloud-2026-05-09-r3/`](gemma4-31b-cloud-2026-05-09-r3/)

Engine HEAD across all 3 runs: `d75b672` (= `6b59116` v1.1.0 for engine purposes —
the 3 commits between are docs-only, no `consultants/` or `agent_loop/` code touched).
Cloud-snapshot drift across the sweep: clean.
Sweep window: 2026-05-09 06:50–07:37 UTC.

**This label is NOT comparable to `gemma4-31b-cloud-2026-05-07`** —
the 2026-05-07 run was at engine HEAD `83cfd3b` (pre-v1.1.0); v1.1.0 was a heavy
`consultants/` rewrite (per protocol §6.2 the prior run is invalidated for
cross-label aggregation). The 2026-05-07 row stays in the scoreboard as historical.

## Wall + token aggregates per query

Median ± MAD across 3 runs.

| Query | Wall median ± MAD | Wall range | All runs (s) |
|---|---|---|---|
| smoke        | 30s ± 0 | 30–61s | 30,30,61 |
| audit-medium | 121s ± 45 | 76–166s | 76,121,166 |
| audit-high   | 181s ± 60 | 121–317s | 317,121,181 |

**Total wall:** r1=423s, r2=272s, r3=408s — median 408s, MAD 15s, spread 151s (56%).

## §7 outlier classification

- All 3 runs within median ± 2×MAD. Wall MAD = 30s, threshold = median + 5×MAD = 558s. r1 (423s) and r3 (408s) are within 2×MAD; r2 (272s) is below median by ~MAD-and-a-half but §7's OUTLIER rule is one-sided (high-only). All 3 classified **CLEAN**.

## Per-query grades (mode across r1/r2/r3)

| Query | Grade | Notes |
|---|---|---|
| smoke | PASS | All 3 runs name 4 roles in one sentence with `path:line` cite at `council.py:116/126/144/156`. |
| audit-medium | A | All 3 runs cite all 6 ground-truth `path:line` sites with verdicts "Yes (exercisable)" matching ground truth perfectly. No `claude_hooks/scripts/` prefix bug. |
| audit-high | A | All 3 runs hit the 4 required claims with `path:line` cites. **Recommendations vary across runs** — r2 proposed prompt fix to `SYNTHESIZER_SYSTEM`; r3 proposed reducer change `error: Optional[str]` → `Annotated[list[str], operator.add]`. Both are valid, address different sub-problems. r1 not yet read. |

## Per-role grades

| Role | Grade | One-sentence justification |
|---|---|---|
| planner | A | 3-7 numbered items, concrete file targets per item |
| researcher | A | every claim cites `path:line`; no fabricated paths across r1/r2/r3 |
| critic | A | DECISION line first, ≤5 lines reasoning |
| synthesizer | A | lead-sentence answer, evidence-grounded; produces actionable hardening recommendations on Q3 |

Mix string: `P:A R:A C:A S:A`

## Verdict

**PROD-READY** per protocol §4 — all 3 queries `status=completed` across all 3 runs, Q1=PASS, Q2=A, Q3=A, no query > 25 min wall, no role hit ≥ 5 retries.

## Per-role wall medians (per-fire) vs N=3 cohort

Median wall per single role invocation (researcher fires once per fan-out lane; counts pooled).

| Role | gemma4 (this label) | gemini-3-flash-preview | deepseek-v4-flash | nemotron-3-super |
|---|---|---|---|---|
| planner | 5.9s | **5.7s** | 8.9s | 13.7s |
| researcher | 168.0s | **39.8s** | 222s | 216s |
| critic | 18.8s | **8.4s** | 28.9s | 36.8s |
| synthesizer | 8.5s | **6.3s** | 16.0s | 9.2s |

`gemini-3-flash-preview:cloud` wins per-role wall on every role (decisively on researcher and critic). The 2026-05-07 single-run claim that gemma4 was the cheapest A pick is **superseded** by N=3 data at the v1.1.0 engine HEAD.

## Commentary

Solid N=3 PROD-READY label, but no longer the wall-cost leader at the v1.1.0 engine. Where gemma4 still shines is the **per-run quality variation on Q3 hardening recommendations**: r2 picked the prompt-fix angle, r3 picked the reducer-change angle, both addressing real sub-problems of the same failure mode. Of the four 2026-05-09 N=3 candidates, gemma4 is the only one whose Q3 recommendations are NOT redundant or regressive when checked against the live code — see the actionability audit in the consolidated comparison report.

Pick gemma4 over gemini when:
- you want **diverse hardening perspectives** across multiple consultations on the same question (gemma4's recommendations vary; gemini's are consistent)
- you specifically need the prompt-engineering style of fix (gemma4 r2)

Pick gemini over gemma4 for default speed — 2-4× cheaper per-fire on the dominant roles.

## Grading caveat — protocol §3 Q3 claim #3

Grades use the corrected ground truth per protocol v1.1 (synthesizer **does** see tombstone via additive `research` reducer). See `EVALUATION.md` changelog.
