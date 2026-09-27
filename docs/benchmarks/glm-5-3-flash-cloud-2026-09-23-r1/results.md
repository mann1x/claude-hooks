# Benchmark — `glm-5-3-flash-cloud-2026-09-23-r1`

Generated 2026-09-23T20:20:13+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `acc1473`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-l2Px`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `glm-5.3-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 15s | 68671 | 955 | 8 | 10 | `csl-2026-09-23-2016-9dc3` |
| audit-medium | medium | completed | 76s | 121491 | 7993 | 17 | 42 | `csl-2026-09-23-2017-dbb5` |
| audit-high | high | completed | 107s | 90394 | 11694 | 28 | 45 | `csl-2026-09-23-2018-0987` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 6.7s | 3 | 25331 | 756 | 4 |
| researcher | 8.5s | 4 | 82892 | 550 | 6 |
| synthesizer | 0.8s | 1 | 2220 | 98 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 32.9s | 4 | 33596 | 4490 | 10 |
| researcher | 16.6s | 10 | 216597 | 5586 | 29 |
| synthesizer | 6.5s | 3 | 15471 | 1446 | 3 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 11.5s | 5 | 18736 | 2022 | 7 |
| researcher | 1.1m | 15 | 443015 | 7650 | 26 |
| critic | 46.2s | 5 | 32945 | 8700 | 7 |
| synthesizer | 9.4s | 3 | 21124 | 2143 | 5 |

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
| audit-medium | **C** | Only migrate:624 and bench_recall:107 judged exercisable; pgvector.py:123/:328 ruled masked, test sites unreachable — 2/6. |
| audit-high | **C+** | All 4 claims (names the two-lane `InvalidUpdateError`); fix is the `runner.py:172-174` status flip. |

Role grades and the verdict are aggregated over r1–r3 in [`../glm-5-3-flash-cloud-2026-09-23/results.md`](../glm-5-3-flash-cloud-2026-09-23/results.md).
