# Benchmark — `gemma4-31b-cloud-2026-09-23-r3`

Generated 2026-09-23T20:50:59+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `acc1473`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-n3hj`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `gemma4:31b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 15s | 48167 | 109 | 6 | 3 | `csl-2026-09-23-2049-91ed` |
| audit-medium | medium | completed | 31s | 121474 | 1835 | 9 | 17 | `csl-2026-09-23-2049-4f6a` |
| audit-high | high | completed | 30s | 96383 | 3045 | 16 | 23 | `csl-2026-09-23-2050-3d74` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.8s | 3 | 19306 | 70 | 2 |
| researcher | 2.5s | 2 | 49086 | 57 | 1 |
| synthesizer | 0.5s | 1 | 1661 | 39 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 2.4s | 3 | 9553 | 305 | 2 |
| researcher | 18.8s | 5 | 180505 | 1636 | 15 |
| synthesizer | 1.8s | 1 | 3207 | 253 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 2.8s | 4 | 9406 | 254 | 3 |
| researcher | 19.2s | 6 | 164754 | 2332 | 16 |
| critic | 3.3s | 5 | 24742 | 287 | 4 |
| synthesizer | 4.4s | 1 | 4189 | 672 | 0 |

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
| smoke | **PASS** | Four roles, one sentence, cited. |
| audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted. |
| audit-high | **B** | Claim 1 missing. Fix: code-shaped relabel of failed lanes in `council.py:253-254` — actionable. |

Role grades and the verdict are aggregated over r1–r3 in [`../gemma4-31b-cloud-2026-09-23/results.md`](../gemma4-31b-cloud-2026-09-23/results.md).
