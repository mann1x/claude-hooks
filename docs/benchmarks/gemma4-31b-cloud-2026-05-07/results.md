# Benchmark — `gemma4-31b-cloud-2026-05-07`

Generated 2026-05-07T09:56:06+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `771bb51`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-s7IY`
Cloud model snapshot: see `models.json`.

Model pin: `gemma4:31b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 121s | 98119 | 904 | 11 | 10 | `csl-2026-05-07-0949-766b` |
| audit-medium | medium | completed | 75s | 119985 | 2659 | 11 | 19 | `csl-2026-05-07-0951-fe97` |
| audit-high | high | completed | 197s | 151764 | 8909 | 13 | 22 | `csl-2026-05-07-0952-b0fc` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 6.6s | 1 | 251 | 310 | 0 |
| researcher | 3.1m | 9 | 216489 | 1095 | 10 |
| synthesizer | 8.9s | 1 | 632 | 408 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 19.9s | 1 | 280 | 569 | 0 |
| researcher | 1.8m | 9 | 247885 | 4519 | 19 |
| synthesizer | 12.5s | 1 | 1997 | 581 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 17.4s | 1 | 424 | 859 | 0 |
| researcher | 4.0m | 10 | 307214 | 9094 | 22 |
| critic | 27.9s | 1 | 3385 | 1134 | 0 |
| synthesizer | 36.0s | 1 | 3555 | 1082 | 0 |

## Files

Per-query artifacts in this directory:

- **smoke**
  - `smoke.summary.md` — synthesizer answer
  - `smoke.transcript.md` — full role transcript
  - `smoke.waterfall.txt` — per-role wall waterfall
  - `smoke.trace.jsonl` — raw JSONL trace
  - `smoke.metadata.json` — token totals + retries
- **audit-medium**
  - `audit-medium.summary.md` — synthesizer answer
  - `audit-medium.transcript.md` — full role transcript
  - `audit-medium.waterfall.txt` — per-role wall waterfall
  - `audit-medium.trace.jsonl` — raw JSONL trace
  - `audit-medium.metadata.json` — token totals + retries
- **audit-high**
  - `audit-high.summary.md` — synthesizer answer
  - `audit-high.transcript.md` — full role transcript
  - `audit-high.waterfall.txt` — per-role wall waterfall
  - `audit-high.trace.jsonl` — raw JSONL trace
  - `audit-high.metadata.json` — token totals + retries

## Per-query grades (per [`EVALUATION.md`](../EVALUATION.md) §3)

| Query | Grade | Notes |
|---|---|---|
| smoke        | PASS | One sentence; all four roles named with `consultants/config.py:40` citation; no hedging |
| audit-medium | A    | All 6 ground-truth psycopg sites cited with correct exercisability verdicts (pgvector.py:123/:328, migrate:624, bench_recall:107, test_pgvector_integration.py:56/:285); install.py protected sites correctly omitted |
| audit-high   | B    | Claims 2-4 present and concrete (researcher_node tombstone at `council.py:555`; synthesizer's missing failure signal; `build_synthesizer_messages` at `council.py:245` as the hardening target); claim 1 (non-additive `error`/`_role_failed` reducers) implicit but not explicitly called out as last-write-wins |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 6 numbered items, each citing a concrete file/path target (`consultants/engine/graph.py`, `runner.py`, `storage.py`) plus a specific verification step ("trace the call stack", "verify if a node failure terminates the entire stream"); no vague "look at how X works" items |
| researcher  | A | Tight per-lane reports across 3 fan-out lanes; every claim cites `path:line`; no hallucinated paths; planner's 6 items covered without redundant reads |
| critic      | A | Single concise paragraph (~5 lines) with parseable `DECISION: ready` line; correctly identifies the trace as complete and the recommendation as cited; no re-route burned on already-complete evidence |
| synthesizer | A | Bottom-line lead ("degraded but coherent answer, but the overall consultation is marked as `status=failed`"); structured per-stage trace with file/line on each claim; recommendation cites real file with concrete change shape; no preamble or hedging |

**Mix string:** `P:A R:A C:A S:A`

**Verdict:** PROD-READY (single-run; pending N=3 confirmation per [§5](../EVALUATION.md#5-multi-run-requirement))

**Commentary:** Across the board strong — gemma4:31b-cloud holds the same `P:A R:A C:A S:A` mix as the kimi baseline at roughly **25% of the wall time** (audit-high 197s vs kimi's 787s). The Q3 grade dropped to B because the synthesizer never explicitly named the non-additive last-write-wins behavior of `error`/`_role_failed` — the implication is there in the recommendation but not stated, and a frontier-tier answer should call it out. Strongly suitable for any role; if cost-optimizing a heterogeneous mix this is the cheaper drop-in for planner / researcher / synthesizer where kimi was previously default. Critic role is fine here too — the verdict was concise and correctly signaled "ready" rather than burning a re-route on the already-complete trace.
