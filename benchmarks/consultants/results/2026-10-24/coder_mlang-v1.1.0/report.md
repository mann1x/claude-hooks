# Skill-Eval Report — coder_mlang suite v1.1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_mlang` |
| suite_version | `1.1.0` |
| suite_hash | `16a0f6d304e90178...` |
| released | `2026-10-24` |
| run_started_at | `2026-10-24T07:42:06.618532Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `(no judge)` |
| git_commit | `cd71cf3` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | 18 | 100% (18/18) | 100% (18/18) | 19.6s | 8902 | 4.78 | ✅ |
| `deepseek-v4.1-flash:cloud` | 18 | 89% (16/18) | 100% (18/18) | 3.5s | 6298 | 4.94 | ✅ |
| `minimax-m3:cloud` | 18 | 89% (16/18) | 100% (18/18) | 44.8s | 9146 | 4.61 | ✅ |
| `glm-5.3:cloud` | 18 | 83% (15/18) | 100% (18/18) | 6.7s | 5590 | 4.33 | ✅ |
| `glm-5.3-flash:cloud` | 18 | 72% (13/18) | 100% (18/18) | 4.1s | 5358 | 4.00 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**deepseek-v4-pro:cloud** wins the rubric: pass_rate=100%, avg_quality=4.78, median_tokens=8902. Also qualifying: deepseek-v4.1-flash:cloud (89%/4.94), minimax-m3:cloud (89%/4.61), glm-5.3:cloud (83%/4.33), glm-5.3-flash:cloud (72%/4.00).

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### medium

**c-medium-01-longest-valid-parens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 5151 | 5.0 | 26 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 4230 | 5.0 | 28 |
| `glm-5.3-flash:cloud` | PASS | 2 | 3778 | 5.0 | 33 |
| `glm-5.3:cloud` | PASS | 2 | 3639 | 5.0 | 28 |
| `minimax-m3:cloud` | PASS | 3 | 9370 | 5.0 | 29 |

**cpp-medium-01-cycle-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 5903 | 5.0 | 43 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 5167 | 4.0 | 48 |
| `glm-5.3-flash:cloud` | PASS | 2 | 4727 | 3.0 | 44 |
| `glm-5.3:cloud` | PASS | 2 | 4816 | 4.0 | 52 |
| `minimax-m3:cloud` | PASS | 2 | 7563 | 5.0 | 51 |

**csharp-medium-01-trapped-rainwater**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 5154 | 5.0 | 41 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 4834 | 5.0 | 44 |
| `glm-5.3-flash:cloud` | tests-fail | 2 | 4600 | 3.0 | 68 |
| `glm-5.3:cloud` | PASS | 4 | 13885 | 5.0 | 41 |
| `minimax-m3:cloud` | PASS | 2 | 5957 | 4.0 | 72 |

**go-medium-01-koko-bananas**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 4741 | 5.0 | 40 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 4486 | 5.0 | 45 |
| `glm-5.3-flash:cloud` | PASS | 2 | 3955 | 5.0 | 38 |
| `glm-5.3:cloud` | PASS | 2 | 3910 | 5.0 | 39 |
| `minimax-m3:cloud` | PASS | 2 | 5182 | 5.0 | 48 |

**python-medium-01-meeting-rooms**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 4037 | 5.0 | 13 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 3940 | 5.0 | 19 |
| `glm-5.3-flash:cloud` | PASS | 2 | 3334 | 5.0 | 13 |
| `glm-5.3:cloud` | PASS | 2 | 3391 | 5.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 4395 | 5.0 | 16 |

**rust-medium-01-search-rotated**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 4961 | 5.0 | 41 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 4373 | 5.0 | 37 |
| `glm-5.3-flash:cloud` | PASS | 2 | 3850 | 5.0 | 44 |
| `glm-5.3:cloud` | PASS | 2 | 4015 | 5.0 | 56 |
| `minimax-m3:cloud` | PASS | 2 | 8923 | 4.0 | 48 |

### hard

**c-hard-01-quicksort-3way**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 8435 | 4.0 | 59 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 6500 | 5.0 | 63 |
| `glm-5.3-flash:cloud` | PASS | 2 | 5311 | 5.0 | 58 |
| `glm-5.3:cloud` | tests-fail | 2 | 4985 | 1.0 | 49 |
| `minimax-m3:cloud` | PASS | 2 | 7133 | 5.0 | 88 |

**cpp-hard-01-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 9672 | 4.0 | 120 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 6081 | 5.0 | 72 |
| `glm-5.3-flash:cloud` | PASS | 2 | 5406 | 5.0 | 71 |
| `glm-5.3:cloud` | PASS | 2 | 5349 | 5.0 | 61 |
| `minimax-m3:cloud` | PASS | 2 | 11315 | 5.0 | 113 |

**csharp-hard-01-async-debounce**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 18600 | 4.0 | 122 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 11145 | 5.0 | 127 |
| `glm-5.3-flash:cloud` | PASS | 2 | 7597 | 4.0 | 110 |
| `glm-5.3:cloud` | PASS | 2 | 6902 | 4.0 | 107 |
| `minimax-m3:cloud` | PASS | 2 | 18277 | 5.0 | 166 |

**go-hard-01-shortest-path-k-stops**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 9369 | 5.0 | 44 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 6701 | 5.0 | 62 |
| `glm-5.3-flash:cloud` | PASS | 2 | 5770 | 5.0 | 45 |
| `glm-5.3:cloud` | PASS | 2 | 5832 | 5.0 | 56 |
| `minimax-m3:cloud` | PASS | 2 | 7961 | 5.0 | 88 |

**python-hard-01-lru-cache**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 4934 | 5.0 | 50 |
| `deepseek-v4.1-flash:cloud` | PASS | 2 | 5065 | 5.0 | 62 |
| `glm-5.3-flash:cloud` | PASS | 2 | 4251 | 5.0 | 52 |
| `glm-5.3:cloud` | PASS | 2 | 4240 | 5.0 | 53 |
| `minimax-m3:cloud` | PASS | 2 | 5998 | 5.0 | 70 |

**rust-hard-01-iter-window-pairs**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | PASS | 2 | 6655 | 5.0 | 47 |
| `deepseek-v4.1-flash:cloud` | tests-fail | 2 | 6096 | 5.0 | 52 |
| `glm-5.3-flash:cloud` | tests-fail | 2 | 4476 | 1.0 | 42 |
| `glm-5.3:cloud` | tests-fail | 2 | 4515 | 1.0 | 44 |
| `minimax-m3:cloud` | tests-fail | 2 | 5930 | 1.0 | 50 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models deepseek-v4-pro:cloud,deepseek-v4.1-flash:cloud,glm-5.3-flash:cloud,glm-5.3:cloud,minimax-m3:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`16a0f6d304e9` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
