# Benchmark — `gemini-3-flash-preview-cloud-2026-05-09-screening`

Generated 2026-05-09T06:18:08+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6b59116`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-jYrZ`
Cloud model snapshot: see `models.json`.

Model pin: `gemini-3-flash-preview:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 31s | 94935 | 3041 | 13 | 10 | `csl-2026-05-09-0614-368c` |
| audit-medium | medium | completed | 45s | 243258 | 7707 | 17 | 24 | `csl-2026-05-09-0615-2fb1` |
| audit-high | high | completed | 136s | 175050 | 22359 | 30 | 28 | `csl-2026-05-09-0615-63b9` |

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
| smoke        | PASS               | names all four roles in one sentence with `path:line` cites at `config.py:40` and `council.py:98-99`; no hedging |
| audit-medium | A                  | all 6 ground-truth `path:line` sites cited with correct verdicts and a one-sentence rationale per site; no `claude_hooks/scripts/` prefix bug; correctly omits the protected install.py sites |
| audit-high   | A-                 | all 4 required claims correct with `path:line` cites; recommendation is code-shaped (`graph.py:69` change reducer to `max`) but targets `research_rounds_used` rather than the failure mode the answer described — slight ding |

## Per-role grades

Per-role grading per protocol §3.5; mode across the 3 queries.
Critic only fires at `effort=high`, so only the audit-high run informs that grade.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner      | A                  | concrete numbered items each with a verification step or file target |
| researcher   | A                  | every claim cites `path:line`; tool calls reasonably batched |
| critic       | A                  | DECISION line first, ≤5 lines reasoning |
| synthesizer  | A                  | lead-sentence answer, evidence-grounded, output shape matches question |

Mix string: `P:A R:A C:A S:A`

## Verdict

**PROD-screen** — promoted to N=3 sweep.

## One-paragraph commentary

Best wall-vs-quality combo of the seven models. Same speed class as nemotron-3-nano but substantively stronger on every query. The only nit is the Q3 hardening recommendation — code-shaped and well-cited but addresses an adjacent reducer rather than the exception path the answer just traced. Top candidate for a full N=3 sweep.

## Grading caveat — protocol §3 Q3 claim #3

The protocol's Q3 rubric claim #3 ("synthesizer's input messages do NOT include the error / failure tombstone") reflects pre-fix behavior. Current code at the baseline tag (`bench-baseline-2026-05-07`) intentionally appends an `(researcher lane failed: …)` tombstone string to the `research` list (additive `operator.add` reducer at `consultants/engine/graph.py`), which `build_synthesizer_messages` (`council.py:245+`) then embeds in the synthesizer's user prompt. The in-code comment at `council.py:555-563` confirms the tombstone-visibility was the fix to a previous audit-high finding. Q3 grades above use the corrected ground truth.
