# Benchmark — `mix-glm-R-gemma4-PCS-2026-10-27` (N=3)

Mix: `glm-5.3-flash:cloud` researcher; `gemma4:31b-cloud` planner, critic and synthesizer. Runs: [`r1`](../mix-glm-R-gemma4-PCS-2026-10-27-r1/results.md), [`r2`](../mix-glm-R-gemma4-PCS-2026-10-27-r2/results.md) (smoke + audit-medium), [`r2b`](../mix-glm-R-gemma4-PCS-2026-10-27-r2b/results.md) (audit-high re-run), [`r3`](../mix-glm-R-gemma4-PCS-2026-10-27-r3/results.md).
Engine HEAD `2dbbe40` — the 2026-10-27 sweep's engine plus the reasoning-leak split (`split_leaked_reasoning`), which never fired here: glm only researched, and the researcher made no thinking-off call (the two forced calls were gemma4's planner). Subject `bench-baseline-2026-05-07` with the answer key removed, 13:46–15:18 UTC, runs 10 min apart, model snapshot verified before r2 and r3. Direct cloud route (ollama.com; no host's Ollama), bench engine on :38195 with its own HOME, no extra lanes, store off. Protocol [`EVALUATION.md`](../EVALUATION.md) 1.4.

**Why this mix:** on the [all-roles sweep](../council-role-sweeps.md#2026-10-27--gemma4-vs-glm-53-flash-every-role-direct-to-ollamacom) glm-5.3-flash's researcher stated claim 1 (`error` / `_role_failed` are last-value channels) in every run and gemma4's never did, while gemma4 was the stronger planner. The question was whether glm's evidence, synthesized by gemma4, gives a better Q3.

| Query | Grade (mode) | Per run | Median wall (uncontended runs) |
|---|---|---|---|
| smoke | **PASS** | PASS / PASS / PASS | 15–31 s |
| audit-medium | **B** | B / B / B | 76–91 s |
| audit-high | **C+** | C+ / C+ (r2b) / C+ | 91 s (90 / 91 / 152) |

Cost per run (list price, Appendix A, no cache discount): $0.041–0.048 (median $0.046; r2 = its smoke and audit-medium + r2b).

**Contention, r2.** A giano xhigh council started on the production engine at 13:46 UTC, the same minute as r1, and shared the Ollama Cloud account (it held no `ollama-cloud` claim). r2's smoke and audit-medium walls (243 s, 637 s) are queueing, not the mix, and are left out of the walls above. r2's audit-high **failed** (FLAP, §9): both glm researcher lanes ended on `startup_stall` — no token at all in the 120 s after the call was admitted to a connection slot, twice each, so ollama.com held the requests rather than this host's slot queue (which `on_admitted` already keeps off the stall clock). It was re-run alone as `r2b` once no council was in flight: 91 s, completed.

## Per-query grades

| Run | Query | Grade | Notes |
|---|---|---|---|
| r1 | smoke | **PASS** | One sentence, cited. |
| r1 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted; `install.py` named as the fallback. |
| r1 | audit-high | **C+** | All four claims — claim 1 (`graph.py:74`, last-write-wins) reaches the answer; fix: `failed` only for a planner/synthesizer failure or empty answer (`runner.py:174`) — the status flip. |
| r2 | smoke | **PASS** | One sentence, one cite per role. |
| r2 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted. |
| r2b | audit-high | **C+** | All four claims (`graph.py:74-75`); status flip at `runner.py:174`. |
| r3 | smoke | **PASS** | One sentence, cited. |
| r3 | audit-medium | **B** | 4/6 (123, 328, 624, 107); test sites omitted. |
| r3 | audit-high | **C+** | All four claims; status flip at `runner.py:174`, "or introduce a degraded status". |

## Per-role grades

| Role | Grade | Justification |
|---|---|---|
| planner (gemma4) | **A** | Concrete, file-targeted plans, as on the all-roles label. |
| researcher (glm-5.3-flash) | **A** | Claim 1 in every Q3; dense citations; misses both Q2 test sites every run (as glm did alone). |
| critic (gemma4) | **B** | `DECISION: ready` first and cited, long. |
| synthesizer (gemma4) | **A** | Bottom line first, short, cited — and carries claim 1 forward every time. |

**Mix string:** `P:A R:A C:B S:A`

## Verdict

**EVALUATED-ONLY** — Q3 below B (§4).

## Commentary

The mix does what it was built to do and it does not help: glm's researcher puts claim 1 in front of the synthesizer and gemma4 states it in every answer — the first label on this engine to carry all four claims with gemma4 writing — yet all three answers then choose the status relabel at `runner.py:174`, the fix gemma4 alone chose in two of three runs. The fix choice follows the evidence: once the answer explains that a lane failure stamps a usable answer `failed`, every model tested (gemma4, glm-5.3-flash, minimax-m3.1-flash) proposes changing the stamp. Q2 is B, not gemma4's A: the researcher decides what is found, and glm's leaves out the test sites. Cost and wall sit between the two single-model labels (~$0.046, ~3 min for all three queries). No role changes.
