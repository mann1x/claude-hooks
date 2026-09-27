# Benchmark — `deepseek-v4-pro-cloud-2026-09-23-screening`

Generated 2026-09-23T18:22:55+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `d2d54ad`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-0ZSm`
Cloud model snapshot: see `models.json`.

Model pin: `deepseek-v4-pro:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 31s | 67996 | 829 | 9 | 10 | `csl-2026-09-23-1814-541c` |
| audit-medium | medium | completed | 152s | 200005 | 12799 | 20 | 60 | `csl-2026-09-23-1815-3041` |
| audit-high | high | completed | 303s | 138601 | 18547 | 27 | 61 | `csl-2026-09-23-1817-4d32` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 5.3s | 2 | 9433 | 464 | 2 |
| researcher | 12.8s | 5 | 111782 | 804 | 7 |
| synthesizer | 2.5s | 2 | 4848 | 136 | 1 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 15.3s | 5 | 26593 | 1358 | 8 |
| researcher | 1.1m | 10 | 377179 | 11705 | 38 |
| synthesizer | 50.5s | 5 | 82204 | 5413 | 14 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.1m | 5 | 38275 | 6632 | 9 |
| researcher | 3.4m | 14 | 551960 | 18350 | 33 |
| critic | 1.4m | 5 | 36500 | 7085 | 11 |
| synthesizer | 27.8s | 3 | 23962 | 2815 | 8 |

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
| smoke | **PASS** | All four roles, one sentence, cited. |
| audit-medium | **A** | All 6 ground-truth sites with correct verdicts; protected and inline-script `install.py` sites correctly excluded. |
| audit-high | **C+** | All 4 claims cited, clean trace; recommendation is the `runner.py:171-174` status flip, the same change it proposed in May (C+). |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner | **A** | Concrete plans on smoke and audit-high; audit-medium opens with a vague 'locate the repository root' (B), mode A. |
| researcher | **A** | Cited, batched (38 / 10, 33 / 14), only 4 linter flags. |
| critic | **A** | `DECISION: ready`, three lines, cited. |
| synthesizer | **A** | Bottom line first, cited, code-shaped recommendation. |

**Mix string:** `P:A R:A C:A S:A`

**Verdict:** EVALUATED-ONLY (Q3 C+)

**Commentary:** Best auditor of today's four on Q2 and the only one with all-A role grades, at $0.66 — 13× glm-5.3-flash and the most expensive label of the day — and the slowest (486 s). Q3 repeats its May status-flip recommendation, so its Q3 ceiling is unchanged.

**Protocol caveats.** Screening (N=1). Engine HEAD `d2d54ad` differs from the 2026-05-09 sweep (fan-out cap 3 → 2 lanes, among others), so per §6.2 earlier labels are not directly comparable. The subject worktree at `bench-baseline-2026-05-07` contains `docs/benchmarks/EVALUATION.md`, i.e. the Q1/Q2 answer key, and at least two of today's labels found it.
