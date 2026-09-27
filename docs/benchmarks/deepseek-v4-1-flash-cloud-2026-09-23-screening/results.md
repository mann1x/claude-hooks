# Benchmark — `deepseek-v4-1-flash-cloud-2026-09-23-screening`

Generated 2026-09-23T18:14:48+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `d2d54ad`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-2JCu`
Cloud model snapshot: see `models.json`.

Model pin: `deepseek-v4.1-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 30s | 137693 | 3125 | 15 | 34 | `csl-2026-09-23-1810-b954` |
| audit-medium | medium | completed | 76s | 221264 | 13192 | 21 | 66 | `csl-2026-09-23-1811-5628` |
| audit-high | high | completed | 152s | 173852 | 12092 | 32 | 92 | `csl-2026-09-23-1812-8c9a` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 4.6s | 3 | 11966 | 615 | 4 |
| researcher | 20.5s | 9 | 260209 | 4115 | 27 |
| synthesizer | 5.6s | 3 | 11884 | 840 | 3 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 16.7s | 6 | 35105 | 2520 | 8 |
| researcher | 22.9s | 10 | 402378 | 11039 | 45 |
| synthesizer | 26.9s | 5 | 38516 | 5301 | 13 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 9.7s | 5 | 11846 | 1499 | 7 |
| researcher | 3.0m | 18 | 940304 | 29909 | 60 |
| critic | 17.1s | 5 | 48672 | 3071 | 16 |
| synthesizer | 13.2s | 4 | 36404 | 2798 | 9 |

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
| smoke | **PASS** | All four roles with one-line functions and cites, two sentences; verbose for a 'single short sentence' ask but within the ≤3-sentence rule. |
| audit-medium | **C** | Cites all 6 sites but only 2 verdicts match ground truth (migrate:624, bench_recall:107); pgvector.py:123 left unresolved, :328 and both test sites ruled not exercisable. Also found and flagged the answer key in the audited tree (`docs/benchmarks/EVALUATION.md:78-96`). |
| audit-high | **C+** | All 4 claims cited, but the recommendation is the `runner.py:174` status flip (C+ for deepseek-v4-flash/-pro in May), and it misstates the two-lane case as last-write-wins rather than an `InvalidUpdateError` abort. |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner | **A** | Concrete, command-shaped plans; audit-medium ran to 11 items (C for that query), mode A. |
| researcher | **B** | Heavy, well-batched tool use (45 / 10, 60 / 18) and dense citations, but 18 citations flagged as the wrong line. |
| critic | **A** | `DECISION: ready`, three lines, cited. |
| synthesizer | **A** | Bottom line first, cited, well structured; smoke answer longer than asked. |

**Mix string:** `P:A R:B C:A S:A`

**Verdict:** EVALUATED-ONLY (Q2 C, Q3 C+)

**Commentary:** Fastest label of the day (258 s total) at $0.19, 3.7× glm-5.3-flash. The same over-reasoning about shim protection that cost deepseek-v4-flash its Q2 in May costs this one too: it argues most sites are not exercisable in practice. Strong researcher, weak auditor on this suite.

**Protocol caveats.** Screening (N=1). Engine HEAD `d2d54ad` differs from the 2026-05-09 sweep (fan-out cap 3 → 2 lanes, among others), so per §6.2 earlier labels are not directly comparable. The subject worktree at `bench-baseline-2026-05-07` contains `docs/benchmarks/EVALUATION.md`, i.e. the Q1/Q2 answer key, and at least two of today's labels found it.
