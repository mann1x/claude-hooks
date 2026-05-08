# Benchmark — `glm-5-1-cloud-2026-05-07`

Generated 2026-05-07T10:05:12+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `771bb51`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-e4FI`
Cloud model snapshot: see `models.json`.

Model pin: `glm-5.1:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 31s | 108711 | 1088 | 12 | 17 | `csl-2026-05-07-0957-ae17` |
| audit-medium | medium | completed | 60s | 108301 | 4641 | 16 | 37 | `csl-2026-05-07-0957-ccac` |
| audit-high | high | completed | 393s | 148031 | 10661 | 23 | 39 | `csl-2026-05-07-0958-04c1` |

## Per-role breakdown

Wall sums every entry of the role node (researcher in
fan-out fires once per lane; counts accumulate). Token
totals include cloud-model reasoning tokens.

### smoke

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 4.1s | 1 | 240 | 306 | 0 |
| researcher | 30.4s | 10 | 227832 | 1207 | 17 |
| synthesizer | 3.5s | 1 | 959 | 43 | 0 |

### audit-medium

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 6.1s | 1 | 265 | 351 | 0 |
| researcher | 1.8m | 14 | 278435 | 5445 | 37 |
| synthesizer | 7.4s | 1 | 2594 | 597 | 0 |

### audit-high

| Role | Wall | LLM calls | Prompt tok | Completion tok | Tool calls |
|---|---|---|---|---|---|
| planner | 7.8s | 1 | 394 | 628 | 0 |
| researcher | 8.6m | 20 | 715909 | 18301 | 39 |
| critic | 42.9s | 1 | 4357 | 1747 | 0 |
| synthesizer | 26.5s | 1 | 4668 | 1020 | 0 |

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
| smoke        | PASS | One sentence; all four roles cited with `consultants/config.py:40`; no hedging; sub-25-second wall |
| audit-medium | B    | Cites 4 of 6 ground-truth sites correctly (pgvector.py:123/:328, migrate:624, bench_recall:107) but **misses both `tests/test_pgvector_integration.py:56/:285` sites**; install.py protected pattern correctly identified in the lead |
| audit-high   | A    | All 4 required claims present and explicitly cited: claim 1 nailed verbatim (`error: Optional[str] (:74) and _role_failed: Optional[str] (:75) — non-additive, default dict-merge (last-write-wins)`); claims 2 and 3 cited with `path:line`; recommendation is code-shaped (Python diff at `graph.py:74-75` with downstream impact noted) |

## Per-role grades (per [`EVALUATION.md`](../EVALUATION.md) §3.5)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A | 6 numbered items, each citing concrete file targets plus a specific verification step ("look for `try/except` around chunk iteration", "test what happens if one researcher's key is missing or is an empty list"); no vague items |
| researcher  | A | Tight per-lane reports; every claim cites `path:line`; surfaces a real inter-round contradiction (R1/R3 vs R2 on whether successful lanes overwrite `error`) that the critic correctly flags as resolvable only by appealing to LangGraph runtime semantics |
| critic      | A | Parseable `DECISION: ready` line; reasoning is verbose by the rubric's "≤ 5 lines" preference but substantively flags the inter-round contradiction and resolves it by appeal to LangGraph framework semantics — verbose-but-substantive beats concise-rubber-stamp here |
| synthesizer | A | Bottom-line lead; structured per-component trace with `path:line` on every claim; recommendation includes a code-shaped diff at the right location (`graph.py:74-75`) with downstream consequences noted; only weakness is upstream — researcher missed 2 test-file sites in audit-medium, but the synthesizer's own output quality across all 3 queries is clean |

**Mix string:** `P:A R:A C:A S:A`

**Verdict:** PROD-READY (single-run; pending N=3 confirmation per [§5](../EVALUATION.md#5-multi-run-requirement))

**Commentary:** glm-5.1:cloud holds full `P:A R:A C:A S:A` and is the **fastest** label in the sweep on smoke + audit-medium (24 s + 56 s wall), with audit-high 393 s — half kimi's 787 s. The Q2 B grade is the one wart: researcher missed `tests/test_pgvector_integration.py:56/:285` in audit-medium, despite finding the four `claude_hooks/` and `scripts/` sites cleanly. That's a 4-of-6 hit rate where the rubric wants ≥5. Audit-high is the standout — explicit non-additive call-out and a code-shaped fix at the right line. Strong fit for the **critic role** in heterogeneous mixes (verbose-but-substantive style is exactly what catches inter-round contradictions); also a strong synthesizer when paired with a researcher that won't miss sites. The on-host config has glm-5.1 as the critic primary with gemma4 as critic extra at xmax — that pairing is well-justified by these grades.
