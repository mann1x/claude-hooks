# Benchmark — `glm-5-3-flash-cloud-2026-09-23-r3`

Generated 2026-09-23T20:45:41+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `acc1473`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-m9iJ`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `glm-5.3-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 30s | 51971 | 1234 | 12 | 14 | `csl-2026-09-23-2041-e86f` |
| audit-medium | medium | completed | 76s | 126702 | 8121 | 17 | 48 | `csl-2026-09-23-2042-4194` |
| audit-high | high | completed | 122s | 106914 | 11412 | 28 | 51 | `csl-2026-09-23-2043-ae24` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 5.8s | 3 | 20233 | 961 | 3 |
| researcher | 16.4s | 7 | 122779 | 852 | 9 |
| synthesizer | 2.0s | 2 | 5153 | 215 | 2 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 13.2s | 3 | 18560 | 2644 | 7 |
| researcher | 37.3s | 9 | 276244 | 5665 | 34 |
| synthesizer | 22.0s | 5 | 31397 | 2424 | 7 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 12.9s | 5 | 17709 | 1385 | 7 |
| researcher | 1.9m | 17 | 622235 | 11524 | 31 |
| critic | 30.5s | 5 | 41207 | 6126 | 13 |
| synthesizer | 6.9s | 1 | 5721 | 1851 | 0 |

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
| audit-medium | **C** | 123/328 'graceful by design', scripts 'partially exempt', tests safe — at most 2–3/6 verdicts match. |
| audit-high | **C+** | All 4 claims (names `InvalidUpdateError`); fix is the `runner.py:174` status flip, argued against stripping the tombstone `error`. |

Role grades and the verdict are aggregated over r1–r3 in [`../glm-5-3-flash-cloud-2026-09-23/results.md`](../glm-5-3-flash-cloud-2026-09-23/results.md).
