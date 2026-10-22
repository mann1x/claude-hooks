# tool_executor skill-eval — 1.0

_harness 1.0 · suite tool_executor@1.0 · hash `7921555c7f36` · mode **live**_
_proxy http://192.168.178.161:11434_
_judge `gemma4:31b-cloud`_

## Per-model summary

| Model | Trials | Passes | Pass rate | Avg quality | Avg tool calls | Avg wall (s) |
|-------|-------:|-------:|----------:|------------:|---------------:|-------------:|
| `minimax-m3.1-flash:cloud` | 24 | 18 | 75.0% | 4.79 | 4.6 | 7.9 |

## Per-tier breakdown

### easy

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `minimax-m3.1-flash:cloud` | 6 | 6 | 100.0% | 5.00 |

### hard

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `minimax-m3.1-flash:cloud` | 6 | 6 | 100.0% | 5.00 |

### medium

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `minimax-m3.1-flash:cloud` | 6 | 0 | 0.0% | 4.17 |

### trivial

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `minimax-m3.1-flash:cloud` | 6 | 6 | 100.0% | 5.00 |

## Rubric

A model qualifies for the tool_executor default iff `pass_rate ≥ 0.70` AND `avg_quality ≥ 3.50`. Among qualifying models the recommended default is the one with the highest pass rate (ties break on median tokens).

## Failures

| Question | Model | Error | Tool calls | Tail of test output |
|----------|-------|-------|-----------:|----------------------|
| medium-01-multifile-audit | `minimax-m3.1-flash:cloud` | — | 6 | 1 failed, 5 passed in 0.03s |
| medium-01-multifile-audit | `minimax-m3.1-flash:cloud` | — | 5 | 1 failed, 5 passed in 0.03s |
| medium-01-multifile-audit | `minimax-m3.1-flash:cloud` | — | 6 | 1 failed, 5 passed in 0.03s |
| medium-02-redundancy-test | `minimax-m3.1-flash:cloud` | — | 6 | 2 failed, 3 passed in 0.03s |
| medium-02-redundancy-test | `minimax-m3.1-flash:cloud` | — | 6 | 2 failed, 3 passed in 0.03s |
| medium-02-redundancy-test | `minimax-m3.1-flash:cloud` | — | 5 | 2 failed, 3 passed in 0.07s |
