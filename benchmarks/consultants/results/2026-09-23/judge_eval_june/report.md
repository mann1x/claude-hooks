# Judge evaluation — `deepseek-v4.1-flash:cloud` against `kimi-k2.6:cloud`

Generated 2026-09-23 18:58 UTC. 420 trials, 420 base verdicts by the candidate.

## Grading quality

| measure | candidate | reference |
|---|---|---|
| paired verdicts | 356 | 356 |
| mean score | 4.40 | 3.83 |
| **AUC, score vs tests passing** | **0.88** | **0.97** |

Agreement: Spearman ρ 0.58, mean |Δ| 0.63, exact 50%, within one point 90%.

## By subject (candidate − reference)

| subject | n | candidate | reference | offset |
|---|---|---|---|---|
| deepseek-v4-flash:cloud *(own family)* | 49 | 4.39 | 3.96 | 0.43 |
| deepseek-v4-pro:cloud *(own family)* | 53 | 4.55 | 4.00 | 0.55 |
| glm-5.1:cloud | 48 | 4.46 | 3.85 | 0.60 |
| kimi-k2.6:cloud | 53 | 4.66 | 4.19 | 0.47 |
| minimax-m2.7:cloud | 53 | 3.83 | 3.30 | 0.53 |
| minimax-m3:cloud | 50 | 4.58 | 3.92 | 0.66 |
| nemotron-3-super:cloud | 50 | 4.32 | 3.62 | 0.70 |

**Self-bias** (offset on own family − offset on others): **-0.10** points.

## Style affinity

Not measurable: the candidate is not a subject of this run, or too few paired verdicts.

## Style invariance

Score change when only the look of a passing solution changes.

| variant | judge | n | mean Δ | mean abs Δ |
|---|---|---|---|---|
| no_comments | deepseek-v4.1-flash:cloud | 0 | — | — |
| no_comments | kimi-k2.6:cloud | 0 | — | — |
| relayout | deepseek-v4.1-flash:cloud | 0 | — | — |
| relayout | kimi-k2.6:cloud | 0 | — | — |

## Stability

0 solutions judged twice by the candidate: exact —%, within one point —%.

## Speed and cost per verdict

| judge | verdicts | failed | median s | p90 s | $ / verdict |
|---|---|---|---|---|---|
| deepseek-v4.1-flash:cloud | 420 | 0 | 1.8 | 3.6 | 0.0003 |
| glm-5.3-flash:cloud | 420 | 0 | 10.1 | 21.0 | 0.0006 |

The reference's own base verdicts were made by coder_bench before per-call timing existed; its speed here comes from the verdicts this tool asked it for.
