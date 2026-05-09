# Benchmark — `mistral-large-3-675b-cloud-2026-05-09-screening`

Generated 2026-05-09T06:47:39+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6b59116`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-5PKY`
Cloud model snapshot: see `models.json`.

Model pin: `mistral-large-3:675b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 30s | 132518 | 851 | 13 | 18 | `csl-2026-05-09-0633-f416` |
| audit-medium | medium | completed | 725s | 80616 | 1266 | 9 | 17 | `csl-2026-05-09-0634-76f0` |
| audit-high | high | completed | 76s | 131510 | 3413 | 21 | 66 | `csl-2026-05-09-0646-44ea` |

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

## Per-query grades

| Query | Grade | Notes |
|---|---|---|
| smoke        | PASS               | names all four roles cleanly with `council.py:98-99` cite |
| audit-medium | C                  | only 2 of 6 ground-truth verdicts correct: mis-classifies `pgvector.py:123` and `:328` as "not exercisable" (they are), misses both `tests/test_pgvector_integration.py` sites entirely. 12 minutes wall + just 1.3k completion tokens — high latency, sparse output |
| audit-high   | C                  | inverts the failure mechanism — claims exceptions "crash the stream unless caught" (false; researcher_node swallows them internally). Hardening recommendation is `return_exceptions=True` for `compiled.stream` — a fabricated LangGraph parameter that does not exist on `.stream()` |

## Per-role grades

Per-role grading per protocol §3.5; mode across the 3 queries.
Critic only fires at `effort=high`, so only the audit-high run informs that grade.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner      | B                  | items present but several are vague |
| researcher   | C                  | narrow Q2 search, missed `tests/` entirely; on Q3 only 21 LLM calls in 1.3 min |
| critic       | F                  | 1.8s, 280 completion tokens — decision line missing or non-parseable per §3.5 F |
| synthesizer  | C                  | Q3 fabricates a LangGraph parameter — §3.5 calls this F territory, scored C as a courtesy because the path:line itself is real |

Mix string: `P:B R:C C:F S:C`

## Verdict

**REJECT** — not suitable for /consultants use.

## One-paragraph commentary

Surprising weakness for a 675B model. Asymmetric latency profile (725s on Q2, 76s on Q3) suggests it bails on hard reasoning. The `return_exceptions=True` fabrication on Q3 is a showstopper: that parameter doesn't exist on LangGraph's `.stream()` and would mislead any implementer who trusts the recommendation. Size doesn't translate to depth here.

## Grading caveat — protocol §3 Q3 claim #3

The protocol's Q3 rubric claim #3 ("synthesizer's input messages do NOT include the error / failure tombstone") reflects pre-fix behavior. Current code at the baseline tag (`bench-baseline-2026-05-07`) intentionally appends an `(researcher lane failed: …)` tombstone string to the `research` list (additive `operator.add` reducer at `consultants/engine/graph.py`), which `build_synthesizer_messages` (`council.py:245+`) then embeds in the synthesizer's user prompt. The in-code comment at `council.py:555-563` confirms the tombstone-visibility was the fix to a previous audit-high finding. Q3 grades above use the corrected ground truth.
