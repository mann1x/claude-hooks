# Benchmark — `gemma4-31b-cloud-2026-09-23-r1`

Generated 2026-09-23T20:25:15+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `acc1473`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Zddh`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `gemma4:31b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 15s | 48168 | 105 | 6 | 3 | `csl-2026-09-23-2024-e89b` |
| audit-medium | medium | completed | 15s | 148201 | 1529 | 10 | 14 | `csl-2026-09-23-2024-bf8b` |
| audit-high | high | completed | 31s | 109605 | 2989 | 13 | 26 | `csl-2026-09-23-2024-9dca` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 2.4s | 3 | 19308 | 70 | 2 |
| researcher | 2.8s | 2 | 49086 | 57 | 1 |
| synthesizer | 0.5s | 1 | 1661 | 35 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 3.9s | 4 | 22173 | 335 | 3 |
| researcher | 15.7s | 5 | 191972 | 1297 | 11 |
| synthesizer | 1.1s | 1 | 2992 | 204 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 7.4s | 4 | 9409 | 296 | 3 |
| researcher | 19.3s | 6 | 165082 | 2123 | 19 |
| critic | 3.2s | 2 | 19570 | 521 | 4 |
| synthesizer | 2.1s | 1 | 4125 | 645 | 0 |

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
| audit-medium | **B** | 4/6 exercisable (624, 107, both test sites); omits pgvector.py:123/:328. |
| audit-high | **B** | Claims 2–4 cited; never states that `error`/`_role_failed` have no reducer (claim 1). Fix: flag failed lanes in `build_synthesizer_messages` (`council.py:253`) — actionable. |

Role grades and the verdict are aggregated over r1–r3 in [`../gemma4-31b-cloud-2026-09-23/results.md`](../gemma4-31b-cloud-2026-09-23/results.md).
