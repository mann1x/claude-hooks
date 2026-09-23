# Benchmark — `gemma4-31b-cloud-2026-09-23-r2`

Generated 2026-09-23T20:37:51+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `acc1473`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-MMc4`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `gemma4:31b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 16s | 48167 | 109 | 6 | 3 | `csl-2026-09-23-2036-6ea2` |
| audit-medium | medium | completed | 30s | 113586 | 1701 | 10 | 16 | `csl-2026-09-23-2036-f28a` |
| audit-high | high | completed | 30s | 111805 | 2711 | 14 | 27 | `csl-2026-09-23-2037-fc74` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.8s | 3 | 19306 | 70 | 2 |
| researcher | 3.1s | 2 | 49086 | 57 | 1 |
| synthesizer | 0.5s | 1 | 1661 | 39 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 3.7s | 4 | 10664 | 317 | 3 |
| researcher | 21.5s | 5 | 189725 | 1510 | 13 |
| synthesizer | 1.4s | 1 | 3021 | 305 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 2.9s | 5 | 9731 | 280 | 4 |
| researcher | 28.5s | 6 | 178848 | 2140 | 19 |
| critic | 3.4s | 2 | 10528 | 376 | 4 |
| synthesizer | 2.7s | 1 | 4006 | 516 | 0 |

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
| audit-high | **B** | Claim 1 missing. Fix: a louder gap marker at the tombstone (`council.py:562`) — actionable. |

Role grades and the verdict are aggregated over r1–r3 in [`../gemma4-31b-cloud-2026-09-23/results.md`](../gemma4-31b-cloud-2026-09-23/results.md).
