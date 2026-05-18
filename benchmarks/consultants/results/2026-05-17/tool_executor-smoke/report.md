# tool_executor skill-eval — 1.0

_harness 1.0 · suite tool_executor@1.0 · hash `7921555c7f36` · mode **live**_
_proxy http://192.168.178.2:11433_
_judge `gemma4:31b-cloud`_

## Per-model summary

| Model | Trials | Passes | Pass rate | Avg quality | Avg tool calls | Avg wall (s) |
|-------|-------:|-------:|----------:|------------:|---------------:|-------------:|
| `glm-5.1:cloud` | 2 | 2 | 100.0% | 5.00 | 1.5 | 4.8 |

## Per-tier breakdown

### trivial

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `glm-5.1:cloud` | 2 | 2 | 100.0% | 5.00 |

## Rubric

A model qualifies for the tool_executor default iff `pass_rate ≥ 0.70` AND `avg_quality ≥ 3.50`. Among qualifying models the recommended default is the one with the highest pass rate (ties break on median tokens).
