# Benchmark — `qwen3-coder-next-cloud-2026-05-09-screening`

Generated 2026-05-09T06:52:37+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6b59116`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-KnHx`
Cloud model snapshot: see `models.json`.

Model pin: `qwen3-coder-next:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 31s | 128847 | 1215 | 17 | 16 | `csl-2026-05-09-0650-4b7d` |
| audit-medium | medium | completed | 30s | 55831 | 954 | 16 | 12 | `csl-2026-05-09-0650-8ab2` |
| audit-high | high | completed | 91s | 245385 | 3883 | 36 | 56 | `csl-2026-05-09-0651-eaa3` |

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
| smoke        | PASS               | names all four roles with role descriptions and `path:line` cites |
| audit-medium | C                  | only the 2 `pgvector.py` sites cited; explicitly asserts "No other psycopg import sites exist in `claude_hooks/`" — but `scripts/` and `tests/` are at REPO ROOT, not under `claude_hooks/`. Model scoped its search to the wrong subtree and missed 4 of 6 ground-truth sites |
| audit-high   | A                  | all 4 required claims correct with `path:line` cites; concludes `status=completed` false-positive (lane failures return tombstone → `node_error` stays None); recommendation is sharp and minimal — change `return {…}` to `raise` at `council.py:554-573` |

## Per-role grades

Per-role grading per protocol §3.5; mode across the 3 queries.
Critic only fires at `effort=high`, so only the audit-high run informs that grade.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner      | B                  | items present, mostly concrete; some Q2 items vague |
| researcher   | C                  | narrow-scoped on Q2 (missed half the ground truth); good Q3 tool-call shape |
| critic       | A                  | DECISION line clean and concise |
| synthesizer  | A                  | Q3 recommendation is the most concise correct fix; Q2 synthesis follows researcher's flawed scope |

Mix string: `P:B R:C C:A S:A`

## Verdict

**EVAL-only** — on hold pending follow-up.

## One-paragraph commentary

Fastest A on Q3 across the cohort (91s) and the recommendation is the most surgical of any model. But the Q2 search-scope failure is a real risk for any audit-style query where ground truth lives outside the obvious source dir. Worth a follow-up screening with a Q2 variant that explicitly references files in scripts/ and tests/, before committing to N=3.

## Grading caveat — protocol §3 Q3 claim #3

The protocol's Q3 rubric claim #3 ("synthesizer's input messages do NOT include the error / failure tombstone") reflects pre-fix behavior. Current code at the baseline tag (`bench-baseline-2026-05-07`) intentionally appends an `(researcher lane failed: …)` tombstone string to the `research` list (additive `operator.add` reducer at `consultants/engine/graph.py`), which `build_synthesizer_messages` (`council.py:245+`) then embeds in the synthesizer's user prompt. The in-code comment at `council.py:555-563` confirms the tombstone-visibility was the fix to a previous audit-high finding. Q3 grades above use the corrected ground truth.
