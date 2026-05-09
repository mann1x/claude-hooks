# Benchmark — `nemotron-3-super-cloud-2026-05-09-screening`

Generated 2026-05-09T06:32:47+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6b59116`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-AxlY`
Cloud model snapshot: see `models.json`.

Model pin: `nemotron-3-super:cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 121s | 98073 | 3612 | 15 | 13 | `csl-2026-05-09-0619-b615` |
| audit-medium | medium | completed | 212s | 156895 | 6928 | 17 | 17 | `csl-2026-05-09-0621-2dfe` |
| audit-high | high | completed | 485s | 173812 | 14993 | 28 | 22 | `csl-2026-05-09-0624-73a7` |

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
| smoke        | PASS               | names all four roles via bullet list with `path:line` cites at `council.py:117/127/145/157`; slightly more verbose than gemini's one-liner but no hedging and ≤3 sentences total |
| audit-medium | B                  | all 6 ground-truth sites with correct verdicts BUT 4 paths get a fabricated `claude_hooks/scripts/…` and `claude_hooks/tests/…` namespace prefix — line numbers correct, paths point to non-existent dirs |
| audit-high   | A                  | all 4 required claims correct; only model in the screening whose hardening recommendation directly addresses the failure mode it just described — wrap `_wrap_researcher` in `graph.py:145-157` with try/except, with concrete code |

## Per-role grades

Per-role grading per protocol §3.5; mode across the 3 queries.
Critic only fires at `effort=high`, so only the audit-high run informs that grade.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner      | A                  | concrete numbered items with file targets |
| researcher   | B                  | good coverage but introduces the `claude_hooks/scripts/` path-prefix hallucination on Q2 — that's a §3.5 "hallucinated paths" downgrade |
| critic       | A                  | DECISION line + concise reasoning |
| synthesizer  | A                  | lead-sentence answer, evidence-grounded, code-shaped Q3 recommendation |

Mix string: `P:A R:B C:A S:A`

## Verdict

**PROD-screen** — promoted to N=3 sweep.

## One-paragraph commentary

Strongest Q3 recommendation of the cohort — only model that proposes a fix at the actual fault layer (lane exception → wrap researcher) rather than shifting the bug elsewhere. Path-prefix hallucination on Q2 is the one risk: needs verification on an N=3 sweep that this isn't a one-off. 818s wall puts it in the kimi-baseline envelope.

## Grading caveat — protocol §3 Q3 claim #3

The protocol's Q3 rubric claim #3 ("synthesizer's input messages do NOT include the error / failure tombstone") reflects pre-fix behavior. Current code at the baseline tag (`bench-baseline-2026-05-07`) intentionally appends an `(researcher lane failed: …)` tombstone string to the `research` list (additive `operator.add` reducer at `consultants/engine/graph.py`), which `build_synthesizer_messages` (`council.py:245+`) then embeds in the synthesizer's user prompt. The in-code comment at `council.py:555-563` confirms the tombstone-visibility was the fix to a previous audit-high finding. Q3 grades above use the corrected ground truth.
