# Judge evaluation — `deepseek-v4.1-flash:cloud` against `kimi-k2.6:cloud`

Generated 2026-09-23 18:58 UTC. 300 trials, 300 base verdicts by the candidate.

## Grading quality

| measure | candidate | reference |
|---|---|---|
| paired verdicts | 298 | 298 |
| mean score | 4.56 | 4.05 |
| **AUC, score vs tests passing** | **0.90** | **0.91** |

Agreement: Spearman ρ 0.55, mean |Δ| 0.57, exact 52%, within one point 92%.

## By subject (candidate − reference)

| subject | n | candidate | reference | offset |
|---|---|---|---|---|
| deepseek-v4-pro:cloud *(own family)* | 59 | 4.63 | 4.25 | 0.37 |
| deepseek-v4.1-flash:cloud *(own family)* | 60 | 4.63 | 4.13 | 0.50 |
| glm-5.3-flash:cloud | 60 | 4.58 | 3.88 | 0.70 |
| glm-5.3:cloud | 59 | 4.39 | 3.86 | 0.53 |
| minimax-m3:cloud | 60 | 4.58 | 4.10 | 0.48 |

**Self-bias** (offset on own family − offset on others): **-0.13** points.

## Style affinity

Other models' code, by token similarity to the candidate's own solution to the same question (n=179). A judge that rewards its own style shows a residual that rises with similarity.

| similarity tercile | mean similarity | mean residual |
|---|---|---|
| least like its own | 0.41 | 0.51 |
| middle | 0.64 | 0.73 |
| most like its own | 0.82 | 0.48 |

Pearson r(similarity, residual) = **0.02**.

## Style invariance

Score change when only the look of a passing solution changes.

| variant | judge | n | mean Δ | mean abs Δ |
|---|---|---|---|---|
| no_comments | deepseek-v4.1-flash:cloud | 8 | -0.12 | 0.38 |
| no_comments | kimi-k2.6:cloud | 8 | -0.25 | 0.50 |
| relayout | deepseek-v4.1-flash:cloud | 39 | -0.03 | 0.23 |
| relayout | kimi-k2.6:cloud | 38 | -0.16 | 0.42 |

## Stability

20 solutions judged twice by the candidate: exact 60%, within one point 100%.

## Speed and cost per verdict

| judge | verdicts | failed | median s | p90 s | $ / verdict |
|---|---|---|---|---|---|
| deepseek-v4.1-flash:cloud | 367 | 0 | 1.8 | 4.5 | 0.0006 |
| glm-5.3-flash:cloud | 367 | 1 | 10.0 | 22.0 | 0.0006 |
| kimi-k2.6:cloud | 67 | 1 | 81.5 | 180.9 | 0.0147 |

The reference's own base verdicts were made by coder_bench before per-call timing existed; its speed here comes from the verdicts this tool asked it for.
