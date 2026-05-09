# Benchmark — `nemotron-3-nano-30b-cloud-2026-05-09-screening`

Generated 2026-05-09T06:13:36+02:00 on solidpc.
Engine HEAD (`/srv/dev-disk-by-label-opt/dev/claude-hooks`): `6b59116`
Subject baseline: `bench-baseline-2026-05-07` (commit `83cfd3b`) — frozen worktree at `/tmp/claude-hooks-bench-bench-baseline-2026-05-07-jFPp`
Cloud model snapshot: see `models.json`.

Model pin: `nemotron-3-nano:30b-cloud` (every role).

## Summary

| Query | Effort | Status | Wall | Prompt tok | Completion tok | LLM calls | Tool calls | sid |
|---|---|---|---|---|---|---|---|---|
| smoke | medium | completed | 30s | 120384 | 4154 | 17 | 12 | `csl-2026-05-09-0610-0c31` |
| audit-medium | medium | completed | 76s | 146592 | 5867 | 14 | 9 | `csl-2026-05-09-0611-88e9` |
| audit-high | high | completed | 61s | 156033 | 8045 | 36 | 31 | `csl-2026-05-09-0612-2133` |

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
| smoke        | FAIL               | answers "the repository does not specify the four consultant council roles" — they are right there in `consultants/engine/council.py`; synthesizer refused despite evidence available |
| audit-medium | B                  | 5 of 6 ground-truth `path:line` cited correctly; mis-classifies the protected `install.py:2127` as a bug (one false positive on the protected install.py set) |
| audit-high   | C                  | partial coverage of the 4 required claims; identifies reducer + tombstone semantics but recommendation is thin and lacks a concrete code-shaped change |

## Per-role grades

Per-role grading per protocol §3.5; mode across the 3 queries.
Critic only fires at `effort=high`, so only the audit-high run informs that grade.

| Role | Grade | One-sentence justification |
|---|---|---|
| planner      | C                  | items present but vague ("understand the architecture") |
| researcher   | C                  | tool calls execute but coverage misses smoke ground truth |
| critic       | C                  | decision line parses but reasoning hand-waves |
| synthesizer  | F                  | smoke synthesis F per §3.5: "refuses to answer despite available evidence" |

Mix string: `P:C R:C C:C S:F`

## Verdict

**REJECT** — not suitable for /consultants use.

## One-paragraph commentary

Fast model (167s total) but the smoke FAIL is disqualifying — the synthesizer returned a refusal on a question whose answer was right in the codebase the researcher had access to. That's the worst kind of failure for /consultants because the user has no signal that anything is wrong. Q2 is acceptable but the synthesizer's behavior on Q1 means we cannot trust this model on simple questions where evidence is sparse. Not worth a full N=3 sweep.

## Grading caveat — protocol §3 Q3 claim #3

The protocol's Q3 rubric claim #3 ("synthesizer's input messages do NOT include the error / failure tombstone") reflects pre-fix behavior. Current code at the baseline tag (`bench-baseline-2026-05-07`) intentionally appends an `(researcher lane failed: …)` tombstone string to the `research` list (additive `operator.add` reducer at `consultants/engine/graph.py`), which `build_synthesizer_messages` (`council.py:245+`) then embeds in the synthesizer's user prompt. The in-code comment at `council.py:555-563` confirms the tombstone-visibility was the fix to a previous audit-high finding. Q3 grades above use the corrected ground truth.
