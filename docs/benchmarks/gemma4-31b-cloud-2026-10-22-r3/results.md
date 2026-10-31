# Benchmark — `gemma4-31b-cloud-2026-10-22-r3`

Generated 2026-10-22T13:01:35+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `8f69cee`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Dxkr`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `gemma4:31b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 15s | 43101 | 433 | 6 | 6 | `csl-2026-10-22-1259-c387` |
| audit-medium | medium | completed | 31s | 120680 | 2264 | 8 | 11 | `csl-2026-10-22-1259-0386` |
| audit-high | high | completed | 76s | 118959 | 4068 | 15 | 22 | `csl-2026-10-22-1300-1466` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 5.2s | 3 | 19755 | 417 | 2 |
| researcher | 3.2s | 2 | 43757 | 241 | 4 |
| synthesizer | 0.9s | 1 | 1908 | 192 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 6.7s | 3 | 26084 | 1513 | 3 |
| researcher | 16.8s | 4 | 136677 | 1772 | 8 |
| synthesizer | 2.6s | 1 | 3124 | 725 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 8.4s | 5 | 12549 | 1322 | 3 |
| researcher | 47.8s | 6 | 198185 | 4507 | 14 |
| critic | 23.0s | 3 | 21367 | 3344 | 5 |
| synthesizer | 5.3s | 1 | 4868 | 1305 | 0 |

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
