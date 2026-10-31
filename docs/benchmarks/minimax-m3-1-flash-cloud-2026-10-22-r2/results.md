# Benchmark — `minimax-m3-1-flash-cloud-2026-10-22-r2`

Generated 2026-10-22T08:23:16+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6df99ad`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-7fei`
Answer key removed from the worktree: `docs/benchmarks docs/consultants-benchmarks.md`
Cloud model snapshot: see `models.json`.

Model pin: `minimax-m3.1-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 45s | 90779 | 1856 | 13 | 19 | `csl-2026-10-22-0813-0e7f` |
| audit-medium | medium | completed | 136s | 140089 | 9901 | 26 | 55 | `csl-2026-10-22-0813-fdb4` |
| audit-high | high | completed | 424s | 231972 | 23068 | 47 | 82 | `csl-2026-10-22-0816-0e41` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 7.3s | 3 | 26085 | 508 | 3 |
| researcher | 41.7s | 7 | 202690 | 1971 | 12 |
| synthesizer | 7.6s | 3 | 14367 | 448 | 4 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 17.7s | 5 | 40678 | 1175 | 6 |
| researcher | 2.9m | 16 | 610281 | 12588 | 43 |
| synthesizer | 17.4s | 5 | 83643 | 2015 | 6 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 17.0s | 5 | 55253 | 1062 | 6 |
| researcher | 9.6m | 32 | 1911195 | 52734 | 64 |
| critic | 28.2s | 5 | 69942 | 2177 | 6 |
| synthesizer | 32.5s | 5 | 53752 | 2979 | 6 |

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
