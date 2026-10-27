# Benchmark — `glm-5-3-flash-cloud-2026-10-27` (N=3)

Model pin: `glm-5.3-flash:cloud` (every role). Runs: [`r1`](../glm-5-3-flash-cloud-2026-10-27-r1/results.md), [`r2`](../glm-5-3-flash-cloud-2026-10-27-r2/results.md), [`r3`](../glm-5-3-flash-cloud-2026-10-27-r3/results.md).
Engine HEAD `e9fe819`, subject `bench-baseline-2026-05-07` with the answer key removed, 10:26–11:24 UTC, interleaved with [`gemma4-31b-cloud-2026-10-27`](../gemma4-31b-cloud-2026-10-27/results.md) 10 min apart, model snapshot verified before r2 and r3. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4. Direct cloud route (ollama.com itself; no host's Ollama), bench engine on :38195 with its own HOME, every role pinned, no extra lanes, store off.

| Query | Grade (mode) | Per run | Median wall ± MAD |
|---|---|---|---|
| smoke | **PASS** | PASS / PASS / PASS | 30s ± 0 |
| audit-medium | **B** | B / B / B | 91s ± 0 |
| audit-high | **C+** | C+ / C+ / C+ | 197s ± 1 |

Cost per run (list price, Appendix A, no cache discount): $0.058–0.066 (median $0.059).

## Per-query grades

| Run | Query | Grade | Notes |
|---|---|---|---|
| r1 | smoke | **PASS** | One sentence, cited per prompt. |
| r1 | audit-medium | **B** | 3/6 (328, 624, 107); omits 123 and reads both test sites as skips; `install.py` correctly the fallback. |
| r1 | audit-high | **C+** | All four claims, claim 1 included (`graph.py:74-75`); fix: `failed` only on a synthesizer failure or empty answer (`runner.py:174`) — the status flip. |
| r2 | smoke | **PASS** | One sentence. |
| r2 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites called "not a production gap". The answer opens with the synthesizer's own reasoning (leak, see below). |
| r2 | audit-high | **C+** | All four claims; status flip at `runner.py:174`. |
| r3 | smoke | **PASS** | One long sentence. |
| r3 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites "out of scope". |
| r3 | audit-high | **C+** | All four claims; status flip at `runner.py:174`. |

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner | **B** | Seven concrete items marking what is unchecked, but every forced plan (5 of 5) opens with the model's reasoning before the list. |
| researcher | **A** | Dense `path:line` citations, 43–61 tool calls per audit, and the only label here to state claim 1 in every run. |
| critic | **B** | Decisions parse and gaps are named with lines, but three of four forced verdicts open with pages of reasoning before `DECISION:`. |
| synthesizer | **A** | Bottom line first, cited, explicit Unverified sections; one leaked answer (r2 Q2). |

**Mix string:** `P:B R:A C:B S:A`

## Verdict

**EVALUATED-ONLY** — Q3 below B (§4); the same three-of-three status flip as [2026-09-23](../glm-5-3-flash-cloud-2026-09-23/results.md).

## Reasoning leak on forced calls

The planner, critic and synthesizer get three tool turns; the call that asks for their deliverable after the cap runs with thinking off (`FORCED_THINK`). glm-5.3-flash does not stop reasoning when `think: false` — it writes the reasoning into the answer, ending it with `</think>`. 8 of its 9 forced calls leaked (planner 5/5, critic 3/4, synthesizer 1/1); gemma4 made 3 forced calls and leaked none. The same think-off probe gives the same result through the local Ollama relay and through ollama.com directly, so it is the model, not the route. The engine keeps the text as the role's answer; a fix belongs in the chat client (split a reasoning block that ends in `</think>` out of the content), measured after this sweep.

## Commentary

glm-5.3-flash researches better than gemma4 — claim 1 every run, three to four times the tool calls — and still chooses the status relabel every time, so its Q3 does not move from 09-23. It is 3–4× gemma4's wall (~320 s for all three queries) at ~1.5× the cost. Its Q2 errors are about verdicts, not coverage: it finds the test sites and decides they don't count. A mix with glm as researcher and gemma4 as synthesizer would carry claim 1 into the answer; whether gemma4's synthesizer then picks a better fix than either label alone is untested.
