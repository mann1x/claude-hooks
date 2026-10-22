# Benchmark — `minimax-m3-1-flash-cloud-2026-10-22b` (N=3)

Model pin: `minimax-m3.1-flash:cloud` (every role). Runs: [`r1`](../minimax-m3-1-flash-cloud-2026-10-22b-r1/results.md), [`r2`](../minimax-m3-1-flash-cloud-2026-10-22b-r2/results.md), [`r3`](../minimax-m3-1-flash-cloud-2026-10-22b-r3/results.md).
Engine HEAD `473998b` (the capped-role fix `7f7c563`; engine started before r1 and constant across r1–r3), subject `bench-baseline-2026-05-07` with the answer key removed, 14:15–15:14 UTC, runs ≥ 10 min apart, model snapshot verified before r2 and r3. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4.

This is the re-run of [`minimax-m3-1-flash-cloud-2026-10-22`](../minimax-m3-1-flash-cloud-2026-10-22/results.md), whose capped planners, critics and synthesizers were asked for a findings report in place of their plan, verdict or answer (task ch-17).

| Query | Grade (median) | Per run | Morning (pre-fix) |
|---|---|---|---|
| smoke | **PASS** | PASS / PASS / PASS | PASS / PASS / PASS |
| audit-medium | **B** | A / C / B | A / C / A |
| audit-high | **C** | C / B / C | C / C / C |

Cost per run: **unpriced** (free on Ollama on the day).

## The fix, measured

The forced last call fired in the same places as in the morning: 15 forced calls over the three runs (planner 6, critic 4, synthesizer 5). In the morning, all 15 asked for a findings report. In the re-run, all 15 asked for the role's own output: the plan, the `DECISION:` verdict or the final answer. None came back empty. The synthesizer's "Verification status" sections are gone, and its audit-high answers are shorter (3.7–6.4k characters, against 4.8–7.0k).

## Per-query grades

| Run | Query | Grade | Notes |
|---|---|---|---|
| r1 | smoke | **PASS** | One sentence, cited. |
| r1 | audit-medium | **A** | 6/6 sites cited; four production sites exercisable; `:56` correctly reached-then-skipped; `:285` left "unverified". All six `install.py` sites classified as covered. |
| r1 | audit-high | **C** | All four claims, cited. Fix: base the status at `runner.py:174` on whether `final_answer` exists — the contentious UX flip, capped at C. |
| r2 | smoke | **PASS** | One sentence, cited. |
| r2 | audit-medium | **C** | Scoped itself to `claude_hooks/` again: `pgvector.py:123` / `:328` only (2/6). `install.py` sites correctly classified. |
| r2 | audit-high | **B** | All four claims; the critic sent it back once (`needs_more_research`). Fix: fan-out lanes stop writing the reducer-less `error` / `_role_failed` and keep the tombstone in `research` / `turns` (`council.py:563-573`). This names the real two-lane `InvalidUpdateError` path, which the live code later fixed with reducers (`join_errors` / `latest_non_none`). But it also changes the `status` a lane failure produces, so it only partly passes the actionability sub-rubric. |
| r3 | smoke | **PASS** | Two sentences, cited. |
| r3 | audit-medium | **B** | 6/6 cited; four production sites correct. The two test sites are described as "dependency-gated" and "cleanup only", with no exercisability verdict. |
| r3 | audit-high | **C** | All four claims; the same status flip at `runner.py:171-174`. |

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner | **A** | Unchanged from the morning. |
| researcher | **A** | Cited throughout. As in the morning, r2 narrowed Q2 to `claude_hooks/`. |
| critic | **B** | Verdict first every run. 13 / 7+7 / 5 lines against the 5-line bar. The r2 send-back led to the only Q3 fix above C. |
| synthesizer | **A** | Bottom line first, cited, no process sections. Smoke is one sentence (r3: two). |

**Mix string:** `P:A R:A C:B S:A` (morning `P:A R:A C:B S:B`).

## Verdict

**EVALUATED-ONLY** — Q3 below B (§4).

## Commentary

The fix changes how the synthesizer reads (S:B → A), not what the council finds. Q3 still lands on the status relabel in two of three runs. The one run that did better got there because its critic sent the research back.

**Against gemma4 on the same engine** ([`gemma4-31b-cloud-2026-10-22`](../gemma4-31b-cloud-2026-10-22/results.md), reasoning on, pre-fix engine; gemma4's roles rarely reach the cap): PASS / B / C, `P:A R:B C:B S:A`. minimax now matches gemma4 on the synthesizer and is ahead on the researcher. Both are C on Q3, and gemma4 is still several times faster.
