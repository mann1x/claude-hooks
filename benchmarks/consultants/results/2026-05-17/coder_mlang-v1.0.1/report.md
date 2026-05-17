# Skill-Eval Report — coder_mlang suite v1.0.1

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_mlang` |
| suite_version | `1.0.1` |
| suite_hash | `ddef8095311c258d...` |
| released | `2026-05-17` |
| run_started_at | `2026-05-17T06:37:05.287199Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `07ff79c` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `kimi-k2.6:cloud` | 13 | 15% (2/13) | 100% (13/13) | 70.8s | 4719 | 3.69 | ❌ |
| `minimax-m2.7:cloud` | 13 | 15% (2/13) | 100% (13/13) | 25.7s | 3541 | 2.15 | ❌ |
| `deepseek-v4-flash:cloud` | 13 | 8% (1/13) | 100% (13/13) | 22.7s | 3704 | 3.08 | ❌ |
| `deepseek-v4-pro:cloud` | 13 | 8% (1/13) | 100% (13/13) | 17.5s | 3345 | 2.77 | ❌ |
| `glm-5.1:cloud` | 13 | 8% (1/13) | 100% (13/13) | 11.2s | 3218 | 2.83 | ❌ |

## Recommended default for `cfg.roles.coder.model`

no model meets the rubric (pass_rate ≥ 70%, quality ≥ 3.5). Best so far: **kimi-k2.6:cloud** with pass_rate=15%, avg_quality=3.69. The role's default stays at the project-global DEFAULT_MODEL until a follow-up run produces a qualifying candidate.

## Per-question detail

### medium

**cpp-medium-01-cycle-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3726 | 3.0 | 55 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 3086 | 3.0 | 54 |
| `glm-5.1:cloud` | tests-fail | 2 | 2831 | 3.0 | 53 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 4886 | 4.0 | 58 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 4477 | 2.0 | 49 |

**csharp-medium-01-trapped-rainwater**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2178 | 5.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2339 | 4.0 | 29 |
| `glm-5.1:cloud` | PASS | 2 | 1862 | 4.0 | 28 |
| `kimi-k2.6:cloud` | PASS | 2 | 1874 | 4.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 1694 | 4.0 | 22 |

### hard

**c-hard-01-quicksort-3way**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2770 | 3.0 | 32 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 3176 | 4.0 | 55 |
| `glm-5.1:cloud` | tests-fail | 2 | 2601 | — | 53 |
| `kimi-k2.6:cloud` | PASS | 2 | 5861 | 4.0 | 36 |
| `minimax-m2.7:cloud` | PASS | 2 | 3249 | 2.0 | 53 |

**cpp-hard-01-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3247 | 4.0 | 79 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 3345 | 2.0 | 86 |
| `glm-5.1:cloud` | tests-fail | 3 | 5156 | 3.0 | 62 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 4001 | 4.0 | 70 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3097 | 2.0 | 88 |

**csharp-hard-01-async-debounce**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3704 | 2.0 | 100 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 3049 | 2.0 | 72 |
| `glm-5.1:cloud` | tests-fail | 2 | 3325 | 1.0 | 135 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 7161 | 2.0 | 101 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3541 | 1.0 | 99 |

**python-hard-01-lru-cache**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3278 | 5.0 | 63 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 3171 | 5.0 | 62 |
| `glm-5.1:cloud` | tests-fail | 2 | 2640 | 4.0 | 29 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 2311 | 4.0 | 50 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2364 | 4.0 | 27 |

**rust-hard-01-iter-window-pairs**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2574 | 4.0 | 51 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2599 | 2.0 | 47 |
| `glm-5.1:cloud` | tests-fail | 2 | 2636 | 3.0 | 35 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 3226 | 4.0 | 32 |
| `minimax-m2.7:cloud` | tests-fail | 3 | 4944 | 1.0 | 49 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models kimi-k2.6:cloud,glm-5.1:cloud,deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`ddef8095311c` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
