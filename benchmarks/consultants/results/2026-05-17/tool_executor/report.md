# tool_executor skill-eval — 1.0

_harness 1.0 · suite tool_executor@1.0 · hash `7921555c7f36` · mode **live**_
_proxy http://192.168.178.2:11433_
_judge `gemma4:31b-cloud`_

## Per-model summary

| Model | Trials | Passes | Pass rate | Avg quality | Avg tool calls | Avg wall (s) |
|-------|-------:|-------:|----------:|------------:|---------------:|-------------:|
| `glm-5.1:cloud` | 8 | 7 | 87.5% | 4.12 | 2.2 | 11.3 |
| `kimi-k2.6:cloud` | 8 | 7 | 87.5% | 4.50 | 3.4 | 6.7 |
| `gemma4:31b-cloud` | 8 | 7 | 87.5% | 5.00 | 2.6 | 4.9 |
| `qwen3-coder-next:cloud` | 8 | 5 | 62.5% | 4.12 | 4.2 | 15.8 |
| `deepseek-v4-pro:cloud` | 8 | 7 | 87.5% | 4.50 | 3.5 | 7.6 |
| `gemini-3-flash-preview:cloud` | 8 | 6 | 75.0% | 4.50 | 4.4 | 7.7 |

## Per-tier breakdown

### easy

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `glm-5.1:cloud` | 2 | 2 | 100.0% | 5.00 |
| `kimi-k2.6:cloud` | 2 | 2 | 100.0% | 5.00 |
| `gemma4:31b-cloud` | 2 | 2 | 100.0% | 5.00 |
| `qwen3-coder-next:cloud` | 2 | 2 | 100.0% | 4.50 |
| `deepseek-v4-pro:cloud` | 2 | 1 | 50.0% | 5.00 |
| `gemini-3-flash-preview:cloud` | 2 | 2 | 100.0% | 5.00 |

### hard

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `glm-5.1:cloud` | 2 | 2 | 100.0% | 5.00 |
| `kimi-k2.6:cloud` | 2 | 2 | 100.0% | 5.00 |
| `gemma4:31b-cloud` | 2 | 2 | 100.0% | 5.00 |
| `qwen3-coder-next:cloud` | 2 | 2 | 100.0% | 5.00 |
| `deepseek-v4-pro:cloud` | 2 | 2 | 100.0% | 5.00 |
| `gemini-3-flash-preview:cloud` | 2 | 2 | 100.0% | 5.00 |

### medium

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `glm-5.1:cloud` | 2 | 2 | 100.0% | 3.50 |
| `kimi-k2.6:cloud` | 2 | 1 | 50.0% | 3.00 |
| `gemma4:31b-cloud` | 2 | 1 | 50.0% | 5.00 |
| `qwen3-coder-next:cloud` | 2 | 0 | 0.0% | 3.00 |
| `deepseek-v4-pro:cloud` | 2 | 2 | 100.0% | 3.00 |
| `gemini-3-flash-preview:cloud` | 2 | 0 | 0.0% | 3.00 |

### trivial

| Model | Trials | Passes | Pass rate | Avg quality |
|-------|-------:|-------:|----------:|------------:|
| `glm-5.1:cloud` | 2 | 1 | 50.0% | 3.00 |
| `kimi-k2.6:cloud` | 2 | 2 | 100.0% | 5.00 |
| `gemma4:31b-cloud` | 2 | 2 | 100.0% | 5.00 |
| `qwen3-coder-next:cloud` | 2 | 1 | 50.0% | 4.00 |
| `deepseek-v4-pro:cloud` | 2 | 2 | 100.0% | 5.00 |
| `gemini-3-flash-preview:cloud` | 2 | 2 | 100.0% | 5.00 |

## Rubric

A model qualifies for the tool_executor default iff `pass_rate ≥ 0.70` AND `avg_quality ≥ 3.50`. Among qualifying models the recommended default is the one with the highest pass rate (ties break on median tokens).

## Failures

| Question | Model | Error | Tool calls | Tail of test output |
|----------|-------|-------|-----------:|----------------------|
| easy-01-grep-read-chain | `deepseek-v4-pro:cloud` | — | 2 | 1 failed, 6 passed in 0.04s |
| medium-01-multifile-audit | `qwen3-coder-next:cloud` | — | 5 | 2 failed, 4 passed in 0.05s |
| medium-01-multifile-audit | `gemini-3-flash-preview:cloud` | — | 6 | 5 failed, 1 passed in 0.05s |
| medium-02-redundancy-test | `kimi-k2.6:cloud` | — | 2 | 2 failed, 3 passed in 0.04s |
| medium-02-redundancy-test | `gemma4:31b-cloud` | — | 3 | 2 failed, 3 passed in 0.04s |
| medium-02-redundancy-test | `qwen3-coder-next:cloud` | — | 6 | 4 failed, 1 passed in 0.04s |
| medium-02-redundancy-test | `gemini-3-flash-preview:cloud` | — | 4 | 2 failed, 3 passed in 0.05s |
| trivial-01-find-symbol | `qwen3-coder-next:cloud` | — | 2 | 1 failed, 3 passed in 0.04s |
| trivial-02-read-section | `glm-5.1:cloud` | — | 0 | 1 failed, 4 passed in 0.03s |
