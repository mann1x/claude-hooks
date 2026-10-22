# Benchmark — `minimax-m3-1-flash-cloud-2026-10-22` (N=3)

Model pin: `minimax-m3.1-flash:cloud` (every role). Runs: [`r1`](../minimax-m3-1-flash-cloud-2026-10-22-r1/results.md), [`r2`](../minimax-m3-1-flash-cloud-2026-10-22-r2/results.md), [`r3`](../minimax-m3-1-flash-cloud-2026-10-22-r3/results.md).
Engine HEAD `6df99ad` (bench engine started on it before r1, constant across r1–r3), subject `bench-baseline-2026-05-07` with the answer key removed, 05:58–06:31 UTC, model snapshot verified before r2 and r3. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4.

**Deviation from §5:** the runs went back to back (r2 started at r1's end, r3 at r2's), not ≥ 10 min apart. The three coder suites ran beside them on the second cloud slot.

| Query | Grade (mode) | Per run | Median wall ± MAD |
|---|---|---|---|
| smoke | **PASS** | PASS / PASS / PASS | 45s ± 0 |
| audit-medium | **A** | A / C / A | 136s ± 0 |
| audit-high | **C** | C / C / C | 424s ± 105 |

Cost per run: **unpriced** — `minimax-m3.1-flash` is not on the 2026-09-23 price snapshot (Appendix A); the model was free on Ollama on the day of the run.

## Per-query grades

| Run | Query | Grade | Notes |
|---|---|---|---|
| r1 | smoke | **PASS** | Four roles, cited; three sentences (the question asked for one short sentence). |
| r1 | audit-medium | **A** | 6/6 ground-truth sites; `tests/test_pgvector_integration.py:285` called "normally unreachable" (key: exercisable from pytest) — 5/6 correct verdicts; all six `install.py` sites correctly excluded. |
| r1 | audit-high | **C** | All four claims, cited. Fix: report `completed` when a final answer exists (`runner.py:171-174`) — the §3 Q3 "contentious UX flip" pattern, capped at C. |
| r2 | smoke | **PASS** | One long sentence, cited. |
| r2 | audit-medium | **C** | Scoped itself to `claude_hooks/`: verdicts for `pgvector.py:123` / `:328` only; names `scripts/…:624` / `:107` but "not fully classified"; test sites unclassified. |
| r2 | audit-high | **C** | All four claims; same status-flip fix, code-shaped. |
| r3 | smoke | **PASS** | One sentence, cited. |
| r3 | audit-medium | **A** | 6/6 with verdicts (`:285` "conditionally"); all six `install.py` sites classified as protected. |
| r3 | audit-high | **C** | All four claims; same status-flip fix at `runner.py:174`. |

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner | **A** | 7-item plans naming files and line ranges on both audits; r3 audit-high ran to 10 items, mostly "still open — tools are off". |
| researcher | **A** | ~1,100 citations over nine answers, ~2% flagged by the CitationLinter as off by a line (right file each time); 3–4 tool calls per turn; full Q2 coverage in r1 and r3. |
| critic | **B** | `DECISION: ready` first every run, cited, but 10–20 lines against the 5-line bar; r3 added useful `CORRECTIONS`. |
| synthesizer | **B** | Bottom line first and cited, but long, with "Verification status" sections, and wordy on smoke. |

**Mix string:** `P:A R:A C:B S:B`

## Verdict

**EVALUATED-ONLY** — Q3 below B (§4).

## Commentary

Strong on evidence: every run states all four Q3 claims, including the one gemma4 missed in all three runs on 2026-09-23 (`error` / `_role_failed` are last-value channels), and two of three runs find all six Q2 sites. It fails on judgement, not evidence: all three runs pick the same fix — relabel a degraded run `completed` — which the actionability sub-rubric names as a contract change rather than a bug fix. It is also slow and heavy: 3–14× gemma4's wall, ~520k prompt tokens a run, with reasoning on 70–90% of its calls. Worth considering as a researcher in a mix; not as the critic or synthesizer, and not as the council default.

**Against gemma4 on the same engine** ([`gemma4-31b-cloud-2026-10-22`](../gemma4-31b-cloud-2026-10-22/results.md), reasoning on): PASS / B / C, `P:A R:B C:B S:A`. minimax wins on evidence (Q2 A vs B, all four Q3 claims vs three); both are C on Q3, both choosing the status relabel; gemma4 is 3–9× faster per query. The 2026-09-23 gemma4 baseline ran without reasoning (fixed in `c148363`) and should not be compared with this label.
