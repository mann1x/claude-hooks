# Benchmark — `glm-5-3-flash-cloud-2026-09-23-screening`

Generated 2026-09-23T18:10:29+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `d2d54ad`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-9q75`
Cloud model snapshot: see `models.json`.

Model pin: `glm-5.3-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 45s | 48597 | 420 | 8 | 8 | `csl-2026-09-23-1803-3667` |
| audit-medium | medium | completed | 106s | 116296 | 6946 | 19 | 49 | `csl-2026-09-23-1803-9e3b` |
| audit-high | high | completed | 288s | 114862 | 12100 | 27 | 48 | `csl-2026-09-23-1805-0536` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 10.8s | 3 | 31655 | 436 | 4 |
| researcher | 28.6s | 3 | 62197 | 378 | 3 |
| synthesizer | 3.2s | 2 | 4127 | 98 | 1 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 19.8s | 5 | 13103 | 1184 | 9 |
| researcher | 56.8s | 9 | 260822 | 5697 | 29 |
| synthesizer | 30.4s | 5 | 40290 | 2557 | 11 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 21.8s | 5 | 16180 | 1367 | 5 |
| researcher | 3.5m | 15 | 563545 | 11852 | 26 |
| critic | 2.2m | 5 | 46721 | 10635 | 15 |
| synthesizer | 19.0s | 2 | 12080 | 1787 | 2 |

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
| smoke | **PASS** | All four roles, one sentence, cites `consultants/config.py:40`. |
| audit-medium | **B** | 4 of 6 ground-truth sites with correct verdicts (pgvector.py:123/:328, migrate:624, bench_recall:107); rules `tests/test_pgvector_integration.py:56` "not a gap" and omits `:285`; protected `install.py` sites correctly excluded. |
| audit-high | **C+** | All 4 claims cited; also finds a real crash no earlier label found (two lanes failing in one superstep write the reducer-less `error` channel → `InvalidUpdateError`, all lane work discarded). But the fix strips `error`/`_role_failed` from the researcher tombstone, silencing the failure signal — the change qwen3.5 got C for (Q3 actionability audit 2026-05-09). |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner | **A** | 7-item plans with concrete commands/paths on both audits; the smoke planner answered instead of planning (F for that query), mode A. |
| researcher | **B** | Cited throughout and batched on audit-medium (29 tools / 9 calls), less so on audit-high (26 / 15); 14 citations flagged by the linter as pointing at the wrong line. |
| critic | **A** | `DECISION: ready`, three lines, cites `path:line`. |
| synthesizer | **A** | Bottom line first on every query, cited, shape matches the question. |

**Mix string:** `P:A R:B C:A S:A`

**Verdict:** EVALUATED-ONLY (Q3 C+)

**Commentary:** The cheapest label ever measured on this sweep ($0.052, level with gemma4's $0.054–0.071) and competitive on wall (439 s total). It is short of gemma4's PASS / A / A on both audits: Q2 misreads the test-file sites and the Q3 hardening change is status-silencing. Its Q3 trace is the sharpest of today's four — it alone names the multi-lane `InvalidUpdateError` crash with the LastValue mechanism — so the analysis is strong and the recommendation is where it loses. Not a drop-in gemma4 replacement on this evidence; a screening, N=1.

**Protocol caveats.** Screening (N=1). Engine HEAD `d2d54ad` differs from the 2026-05-09 sweep (fan-out cap 3 → 2 lanes, among others), so per §6.2 earlier labels are not directly comparable. The subject worktree at `bench-baseline-2026-05-07` contains `docs/benchmarks/EVALUATION.md`, i.e. the Q1/Q2 answer key, and at least two of today's labels found it.
