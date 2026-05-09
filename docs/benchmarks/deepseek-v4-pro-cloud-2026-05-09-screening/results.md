# Benchmark — `deepseek-v4-pro-cloud-2026-05-09-screening`

Generated 2026-05-09T07:22:38+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6b59116`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-MszE`
Cloud model snapshot: see `models.json`.

Model pin: `deepseek-v4-pro:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 76s | 83766 | 1481 | 10 | 11 | `csl-2026-05-09-0704-73d2` |
| audit-medium | medium | completed | 242s | 353819 | 14385 | 17 | 48 | `csl-2026-05-09-0706-5fb7` |
| audit-high | high | completed | 741s | 159184 | 14322 | 28 | 53 | `csl-2026-05-09-0710-3ecb` |

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
| smoke        | PASS               | names all four roles with `config.py:40` cite |
| audit-medium | C                  | most thorough Q2 reasoning of any model — cites all 6 ground-truth sites with detailed per-site arguments — but only 2 of 6 verdicts match ground truth. Over-reasons about conda-env shim protection layers ("production call chain `bin/` → `_resolve_python.sh` forces conda-env Python") and concludes 4 of 6 sites are "not exercisable". Defensible but wrong by the rubric — same trap as mistral-large-3 |
| audit-high   | A                  | all 4 required claims correct with `path:line` cites; recommendation is a concrete code-diff at `runner.py:172-179` flipping status logic to "completed if non-empty `final_answer`". 14.3k completion tokens, 53 tool calls — heavy investigation |

## Per-role grades

Per-role grading per protocol §3.5; mode across the 3 queries.
Critic only fires at `effort=high`, so only the audit-high run informs that grade.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner      | A                  | very long plans (over-decomposes); items concrete |
| researcher   | B                  | thorough but introduces the "protected by conda shims = not exercisable" framing that hurts Q2 grade |
| critic       | A                  | DECISION line and concise |
| synthesizer  | A                  | best-in-class for code-diff hardening on Q3; verbose preamble on Q2 |

Mix string: `P:A R:B C:A S:A`

## Verdict

**EVAL-only** — on hold pending follow-up.

## One-paragraph commentary

No longer broken on the local proxy as of 2026-05-09 — update memory entry. Operationally heavy at 1059s wall (> 17 min), only worth N=3 if you specifically want a frontier-class synthesizer for code-diff hardening recommendations. Q2 over-reasoning (same pattern as mistral-large-3) makes it untrustworthy for grep-style audits. Hold.

## Grading caveat — protocol §3 Q3 claim #3

The protocol's Q3 rubric claim #3 ("synthesizer's input messages do NOT include the error / failure tombstone") reflects pre-fix behavior. Current code at the baseline tag (`bench-baseline-2026-05-07`) intentionally appends an `(researcher lane failed: …)` tombstone string to the `research` list (additive `operator.add` reducer at `consultants/engine/graph.py`), which `build_synthesizer_messages` (`council.py:245+`) then embeds in the synthesizer's user prompt. The in-code comment at `council.py:555-563` confirms the tombstone-visibility was the fix to a previous audit-high finding. Q3 grades above use the corrected ground truth.
