# Benchmark — `glm-5-3-flash-cloud-2026-09-23-r2`

Generated 2026-09-23T20:32:34+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `acc1473`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-xbWO`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `glm-5.3-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 15s | 27157 | 377 | 7 | 6 | `csl-2026-09-23-2029-5be8` |
| audit-medium | medium | completed | 76s | 150272 | 8293 | 19 | 55 | `csl-2026-09-23-2029-e1eb` |
| audit-high | high | completed | 107s | 99294 | 8776 | 29 | 55 | `csl-2026-09-23-2030-b3da` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 3.8s | 3 | 17104 | 402 | 3 |
| researcher | 2.8s | 2 | 33446 | 227 | 2 |
| synthesizer | 1.4s | 2 | 3922 | 104 | 1 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 25.3s | 4 | 28897 | 3228 | 9 |
| researcher | 38.2s | 10 | 281715 | 5878 | 34 |
| synthesizer | 10.2s | 5 | 32300 | 1897 | 12 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 9.1s | 5 | 21143 | 1573 | 6 |
| researcher | 1.8m | 19 | 621137 | 9272 | 39 |
| critic | 26.4s | 4 | 32011 | 4665 | 10 |
| synthesizer | 6.4s | 1 | 5584 | 1259 | 0 |

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
| audit-medium | **C** | Only :624 and :107 exercisable — 2/6. |
| audit-high | **C+** | All 4 claims; fix is the `runner.py:174` status flip. |

Role grades and the verdict are aggregated over r1–r3 in [`../glm-5-3-flash-cloud-2026-09-23/results.md`](../glm-5-3-flash-cloud-2026-09-23/results.md).
