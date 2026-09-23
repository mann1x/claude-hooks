# Benchmark — `glm-5-3-cloud-2026-09-23-screening`

Generated 2026-09-23T18:27:45+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `d2d54ad`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-7w8z`
Cloud model snapshot: see `models.json`.

Model pin: `glm-5.3:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 15s | 48635 | 934 | 9 | 8 | `csl-2026-09-23-1822-48c2` |
| audit-medium | medium | completed | 107s | 142578 | 6676 | 16 | 45 | `csl-2026-09-23-1823-b6c2` |
| audit-high | high | completed | 167s | 104903 | 12554 | 25 | 42 | `csl-2026-09-23-1824-ef5e` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 6.0s | 3 | 19923 | 655 | 4 |
| researcher | 9.5s | 5 | 86650 | 611 | 4 |
| synthesizer | 0.8s | 1 | 2318 | 63 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 54.4s | 4 | 43944 | 3036 | 8 |
| researcher | 32.4s | 9 | 290150 | 5169 | 31 |
| synthesizer | 8.5s | 3 | 18031 | 1383 | 6 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 56.6s | 5 | 13050 | 1183 | 8 |
| researcher | 1.1m | 13 | 415759 | 7030 | 25 |
| critic | 49.2s | 5 | 31632 | 6282 | 6 |
| synthesizer | 21.3s | 2 | 12552 | 1478 | 3 |

## Files

Per-query artifacts in this directory:

- **smoke**
  - `smoke.summary.md` — synthesizer answer
  - `smoke.transcript.md` — full role transcript
  - `smoke.waterfall.txt` — per-role wall waterfall
  - `smoke.transcript.db` — structured event log (SQLite, v1.1)
  - `smoke.metadata.json` — token totals + retries
- **audit-medium**
  - `audit-medium.summary.md` — synthesizer answer
  - `audit-medium.transcript.md` — full role transcript
  - `audit-medium.waterfall.txt` — per-role wall waterfall
  - `audit-medium.transcript.db` — structured event log (SQLite, v1.1)
  - `audit-medium.metadata.json` — token totals + retries
- **audit-high**
  - `audit-high.summary.md` — synthesizer answer
  - `audit-high.transcript.md` — full role transcript
  - `audit-high.waterfall.txt` — per-role wall waterfall
  - `audit-high.transcript.db` — structured event log (SQLite, v1.1)
  - `audit-high.metadata.json` — token totals + retries

## Per-query grades (per [`EVALUATION.md`](../EVALUATION.md) §3, graded 2026-09-23)

| Query | Grade | Notes |
|---|---|---|
| smoke | **PASS** | All four roles, one sentence — but cites the answer key (`docs/benchmarks/EVALUATION.md:68-70`) as its source alongside `council.py:98`. |
| audit-medium | **A** | All 6 sites with correct verdicts and a correct protected-site analysis — but the planner's step 6 cross-checks against the ground truth in `EVALUATION.md:78`, so the grade is contaminated. |
| audit-high | **B+** | All 4 claims cited, names the multi-lane `InvalidUpdateError` abort; the fix drops `error`/`_role_failed` only on Send lanes and pairs it with an additive `lane_errors` field so the runner can tell 'one lane degraded' from 'role failed' — the additive-reducer shape glm-5.1 got B+ for. |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner | **A** | Concrete, file-and-line plans on all three queries — including steps that read the answer key. |
| researcher | **A** | Cited, batched (31 / 9, 25 / 13), zero linter flags. |
| critic | **A** | `DECISION: ready`, three lines, cited. |
| synthesizer | **A** | Bottom line first, cited, the best-shaped Q3 answer of the day. |

**Mix string:** `P:A R:A C:A S:A`

**Verdict:** PROD-READY (screening) — grades contaminated: read the answer key

**Commentary:** The strongest answers of the day on paper (PASS / A / B+, all roles A) at $0.50 and 289 s, but its planner went looking for the benchmark's own answer key in the audited tree and used it on two of three queries. The Q3 B+ does not depend on the key (the rubric has no ground-truth list for it). Re-run after the answer key is removed from the worktree before trusting Q1/Q2.

**Protocol caveats.** Screening (N=1). Engine HEAD `d2d54ad` differs from the 2026-05-09 sweep (fan-out cap 3 → 2 lanes, among others), so per §6.2 earlier labels are not directly comparable. The subject worktree at `bench-baseline-2026-05-07` contains `docs/benchmarks/EVALUATION.md`, i.e. the Q1/Q2 answer key, and at least two of today's labels found it.
