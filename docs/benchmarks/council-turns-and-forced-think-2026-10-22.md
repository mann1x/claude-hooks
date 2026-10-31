# Council tool turns and forced-call thinking — 2026-10-22

Two measurement arms behind two defaults of the tooled roles (planner, critic, meta-critic, synthesizer, adversary):

- **Tool turns:** 3 tool turns plus one turn to answer (`ROLE_TOOL_ITERS`). The recorded history cannot say how many turns a role would take, because the planner uses all three in ~90% of councils.
- **Forced-call thinking:** the call that writes the role's answer after the cap runs with thinking off.

Engine `d98536c` for every arm. The knobs `CONSULTANTS_ROLE_TOOL_TURNS` and `CONSULTANTS_FORCED_THINK` are read at engine start, with one engine per arm. Subject `bench-baseline-2026-05-07`, protocol [`EVALUATION.md`](EVALUATION.md) 1.4, N=3 per arm, runs 10 min apart, model snapshot verified before r2 and r3. Cost: minimax was free on the day; the gemma4 arms are a few cents each.

| Arm | Label | Setting |
|---|---|---|
| minimax baseline | [`minimax-m3-1-flash-cloud-2026-10-22b`](minimax-m3-1-flash-cloud-2026-10-22b/results.md) | defaults (engine `473998b`; no difference on these paths) |
| minimax t12 | `minimax-m3-1-flash-cloud-2026-10-22t12-r{1..3}` | 12 tool turns |
| minimax ft | `minimax-m3-1-flash-cloud-2026-10-22ft-r{1..3}` | defaults + thinking kept on the forced call |
| gemma4 c | `gemma4-31b-cloud-2026-10-22c-r{1..3}` | defaults |
| gemma4 t12 | `gemma4-31b-cloud-2026-10-22t12-r{1..3}` | 12 tool turns |
| gemma4 ft | `gemma4-31b-cloud-2026-10-22ft-r{1..3}` | defaults + thinking kept on the forced call |

## Tool turns a role uses (9 instances per arm: 3 queries × 3 runs)

| Role | minimax, 3 | minimax, 12 | gemma4, 3 | gemma4, 12 |
|---|---|---|---|---|
| planner | 2, 3×8 | 2, 2, 3, 9, 10, 12×4 | 2×3, 3×6 | 2×4, 3, 4, 7, 7, 8 |
| critic (audit-high) | 3, 3, 3, 3 | 12, 12, 12 | 1, 2, 2 | 1, 1, 3 |
| synthesizer | 1×3, 3×6 | 1, 2, 2, 6, 10, 12×4 | 0×9 | 0×9 |
| prompt tokens (3 runs) | 9.98 M | 15.29 M (+53%) | 1.42 M | 1.81 M (+27%) |

- **minimax has no natural stopping point.** Allowed 12 turns, its critic and its audit planners and synthesizers use all 12.
- **gemma4's planner** stops at 2–3 in 6 of 9 instances; the rest stop at 4, 7, 7 and 8. Its critic stays at 1–3, and its synthesizer never calls a tool.

## Quality

The audit-medium column counts the ground-truth sites cited, not graded verdicts. The audit-high column grades the recommended fix against the actionability sub-rubric; the four required claims were not re-checked for every arm.

| Arm | audit-medium sites (of 6) | audit-high (fix) | audit-high wall |
|---|---|---|---|
| minimax baseline | 6 / 2 / 6 | C / B / C | 425 / 745 / 610 s |
| minimax t12 | 6 / 6 / 6 | C / C / C (status relabel ×3) | 865 / 738 / 561 s |
| minimax ft | 6 / 6 / 6 | B / C / C (lane + plan item in the tombstone; relabel ×2) | 469 / 593 / 488 s |
| gemma4 c | 6 / 4 / 4 | A / B / C (additive `error` reducer; tagged tombstone; redundant try/except) | 49 / 67 / 40 s |
| gemma4 t12 | 4 / 6 / 4 | C / C / C (status relabel; delete the lane's `error` ×2) | 63 / 82 / 90 s |
| gemma4 ft | 4 / 6 / 6 | B / B / B (plan item in the tombstone; warn the synthesizer; louder marker) | 35 / 58 / 28 s |

## Forced-call thinking (minimax: 15 forced calls per 3 runs in both arms)

| | Thinking off (baseline) | Thinking on (ft) |
|---|---|---|
| completion tokens, all forced calls | 13.4 k | 36.4 k (2.7×) |
| median forced call | 822 tok, 10.0 s | 990 tok, 11.6 s |
| time in forced calls | 142 s | 365 s (≈ +75 s a run) |
| empty answers | 0 | 0 |

gemma4 reaches the forced call too rarely to measure: 2 calls in its baseline and 1 in its ft arm, so the gemma4 ft row above is not an effect of thinking.

## Conclusions

- **Keep 3 tool turns.** Since `7f7c563`, the forced call writes the role's own plan, verdict or answer, so reaching the cap costs one turn without thinking. 12 turns cost 27–53% more prompt tokens and left Q3 no better on either model. minimax's Q2 coverage improved, but its baseline miss is the same r2 scoping seen in the morning, and the ft arm also reached 6/6 without extra turns. A planner-only raise to ~6 would cover 6 of 9 gemma4 planners (not the 7–8-turn ones); it is the only change worth another arm.
- **Keep thinking off on the forced call.** Keeping it costs 2.7× the forced call's tokens and about 75 s per minimax run, and shows no measurable gain at N=3. It produced no empty turns. `CONSULTANTS_FORCED_THINK=graceful` remains as the switch for a larger arm.
- N=3 per arm. These are directions, not significance.
