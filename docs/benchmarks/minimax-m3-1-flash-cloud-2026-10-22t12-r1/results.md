# Benchmark — `minimax-m3-1-flash-cloud-2026-10-22t12-r1`

Generated 2026-10-22T17:38:40+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `d98536c`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rwwx`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `minimax-m3.1-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 46s | 91670 | 2546 | 14 | 24 | `csl-2026-10-22-1719-8908` |
| audit-medium | medium | completed | 227s | 182997 | 14154 | 42 | 84 | `csl-2026-10-22-1720-75d8` |
| audit-high | high | completed | 879s | 294722 | 25892 | 82 | 147 | `csl-2026-10-22-1724-d606` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 8.9s | 3 | 24097 | 526 | 3 |
| researcher | 34.9s | 8 | 202831 | 2698 | 16 |
| synthesizer | 5.9s | 3 | 15423 | 626 | 5 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 1.2m | 13 | 374791 | 4772 | 23 |
| researcher | 2.1m | 16 | 543842 | 12623 | 38 |
| synthesizer | 1.3m | 13 | 265029 | 8194 | 23 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 2.6m | 13 | 425948 | 15639 | 22 |
| researcher | 16.4m | 41 | 2424551 | 116707 | 78 |
| critic | 1.3m | 14 | 347121 | 6397 | 24 |
| synthesizer | 1.2m | 14 | 286787 | 4232 | 23 |

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
