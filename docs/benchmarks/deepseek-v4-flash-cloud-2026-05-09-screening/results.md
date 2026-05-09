# Benchmark — `deepseek-v4-flash-cloud-2026-05-09-screening`

Generated 2026-05-09T07:03:58+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6b59116`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Uaj8`
Cloud model snapshot: see `models.json`.

Model pin: `deepseek-v4-flash:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 76s | 82538 | 1017 | 12 | 13 | `csl-2026-05-09-0653-9374` |
| audit-medium | medium | completed | 167s | 167705 | 11132 | 15 | 44 | `csl-2026-05-09-0654-e2ce` |
| audit-high | high | completed | 378s | 163959 | 17649 | 24 | 50 | `csl-2026-05-09-0657-7477` |

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
| audit-medium | B                  | all 6 ground-truth sites cited; 4 of 6 verdicts correct; the 2 wrong ones argue "not exercisable" using sophisticated reasoning about `bin/_resolve_python.sh` shim protection and pytest skip semantics — defensible but contradicts the rubric's ground truth. Paths clean (no spurious `claude_hooks/` prefix). Adds remediation-priority commentary |
| audit-high   | A                  | all 4 required claims correct with `path:line` cites; concludes "coherent (partial) answer + `status=failed`"; recommendation is code-shaped at `runner.py:172-179` (flip status logic to "completed if `final_answer` non-empty"). 17.6k completion tokens on Q3 — solid reasoning depth |

## Per-role grades

Per-role grading per protocol §3.5; mode across the 3 queries.
Critic only fires at `effort=high`, so only the audit-high run informs that grade.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner      | A                  | concrete numbered items with verification steps |
| researcher   | A                  | every claim cites `path:line`; on Q3 fired 50 tool calls across 24 LLM turns |
| critic       | A                  | DECISION line clean |
| synthesizer  | A                  | evidence-grounded, code-shaped recommendation that's testable |

Mix string: `P:A R:A C:A S:A`

## Verdict

**PROD-screen** — promoted to N=3 sweep.

## One-paragraph commentary

Tied with gemini for top role grades but uses ~3x more wall to get there (621s vs 213s). The extra cost shows up as richer per-site reasoning on Q2 and a deeper Q3. Whether the wall trade is worth it depends on the query class — for slow-but-thorough audits this is the better pick; for fast-loop interactive use, gemini wins. Strong N=3 candidate.

## Grading caveat — protocol §3 Q3 claim #3

The protocol's Q3 rubric claim #3 ("synthesizer's input messages do NOT include the error / failure tombstone") reflects pre-fix behavior. Current code at the baseline tag (`bench-baseline-2026-05-07`) intentionally appends an `(researcher lane failed: …)` tombstone string to the `research` list (additive `operator.add` reducer at `consultants/engine/graph.py`), which `build_synthesizer_messages` (`council.py:245+`) then embeds in the synthesizer's user prompt. The in-code comment at `council.py:555-563` confirms the tombstone-visibility was the fix to a previous audit-high finding. Q3 grades above use the corrected ground truth.
