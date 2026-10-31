# Benchmark — `minimax-m3-1-flash-cloud-2026-10-22b-r1`

Generated 2026-10-22T16:25:27+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `473998b`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-PPRS`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `minimax-m3.1-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 46s | 80076 | 1655 | 14 | 22 | `csl-2026-10-22-1615-b76c` |
| audit-medium | medium | completed | 152s | 150480 | 12399 | 26 | 45 | `csl-2026-10-22-1615-81c1` |
| audit-high | high | completed | 425s | 220655 | 22699 | 44 | 71 | `csl-2026-10-22-1618-1e3a` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 11.0s | 4 | 42424 | 476 | 5 |
| researcher | 30.2s | 8 | 185802 | 2097 | 15 |
| synthesizer | 5.0s | 2 | 8533 | 289 | 2 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 15.0s | 5 | 37717 | 919 | 6 |
| researcher | 3.0m | 16 | 675619 | 14292 | 31 |
| synthesizer | 32.5s | 5 | 44669 | 3440 | 8 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 14.0s | 5 | 63070 | 822 | 5 |
| researcher | 10.3m | 29 | 1617236 | 58748 | 54 |
| critic | 26.1s | 5 | 67294 | 1694 | 6 |
| synthesizer | 24.1s | 5 | 50478 | 1922 | 6 |

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

## Per-query grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3)

| Query | Grade | Notes |
|---|---|---|
| smoke        | _PASS / WEAK / FAIL_   | _one-line note_ |
| audit-medium | _A / B / C / F_        | _one-line note_ |
| audit-high   | _A / B / C / F_        | _one-line note_ |

## Per-role grades (manual, per [`EVALUATION.md`](../EVALUATION.md) §3.5)

Read each role's output in the per-query `transcript.md`
files and assign one grade per role aggregated across all
three queries. Critic grade is `n/a` unless audit-high ran.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | _A / B / C / F_      | _why_ |
| researcher  | _A / B / C / F_      | _why_ |
| critic      | _A / B / C / F / n/a_| _why_ |
| synthesizer | _A / B / C / F_      | _why_ |

**Mix string:** `P:_ R:_ C:_ S:_`

**Verdict:** _PROD-READY / EVALUATED-ONLY / UNSTABLE_

**Commentary:** _one paragraph — what role(s) this model wins at vs prior labels, which role(s) it should NOT be used for, whether you'd build a heterogeneous mix around it_
