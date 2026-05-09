# Benchmark — `gemma4-31b-cloud-2026-05-09-r3`

Generated 2026-05-09T09:36:57+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `d75b672`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-wtV5`
Cloud model snapshot: see `models.json`.

Model pin: `gemma4:31b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 61s | 115122 | 1188 | 11 | 12 | `csl-2026-05-09-0930-5a5f` |
| audit-medium | medium | completed | 166s | 178747 | 4011 | 11 | 22 | `csl-2026-05-09-0931-a145` |
| audit-high | high | completed | 181s | 170632 | 8660 | 13 | 25 | `csl-2026-05-09-0933-c549` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 11.3s | 1 | 251 | 353 | 0 |
| researcher | 1.2m | 9 | 243807 | 1472 | 12 |
| synthesizer | 8.5s | 1 | 590 | 299 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 4.2s | 1 | 280 | 521 | 0 |
| researcher | 4.1m | 9 | 331814 | 5835 | 22 |
| synthesizer | 7.3s | 1 | 2161 | 638 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 5.9s | 1 | 424 | 719 | 0 |
| researcher | 4.3m | 10 | 340597 | 9182 | 25 |
| critic | 28.5s | 1 | 3178 | 1044 | 0 |
| synthesizer | 34.0s | 1 | 3402 | 1265 | 0 |

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
