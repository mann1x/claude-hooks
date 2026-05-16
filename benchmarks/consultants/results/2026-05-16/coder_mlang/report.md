# Skill-Eval Report — coder_mlang suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_mlang` |
| suite_version | `1.0` |
| suite_hash | `861046693fa98d58...` |
| released | `2026-05-16` |
| run_started_at | `2026-05-16T16:25:28.316349Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `6b83b09` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 18 | 33% (6/18) | 100% (18/18) | 22.4s | 3010 | 3.33 | ❌ |
| `deepseek-v4-pro:cloud` | 18 | 33% (6/18) | 100% (18/18) | 14.3s | 3017 | 2.83 | ❌ |
| `kimi-k2.6:cloud` | 18 | 33% (6/18) | 100% (18/18) | 45.7s | 4963 | 3.33 | ❌ |
| `glm-5.1:cloud` | 18 | 28% (5/18) | 100% (18/18) | 11.3s | 2827 | 3.28 | ❌ |
| `minimax-m2.7:cloud` | 18 | 22% (4/18) | 100% (18/18) | 16.5s | 3304 | 2.44 | ❌ |

## Recommended default for `cfg.roles.coder.model`

no model meets the rubric (pass_rate ≥ 70%, quality ≥ 3.5). Best so far: **kimi-k2.6:cloud** with pass_rate=33%, avg_quality=3.33. The role's default stays at the project-global DEFAULT_MODEL until a follow-up run produces a qualifying candidate.

## Per-question detail

### medium

**c-medium-01-longest-valid-parens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2677 | 4.0 | 30 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2368 | 3.0 | 24 |
| `glm-5.1:cloud` | PASS | 2 | 1981 | 3.0 | 28 |
| `kimi-k2.6:cloud` | PASS | 2 | 3018 | 4.0 | 38 |
| `minimax-m2.7:cloud` | PASS | 2 | 1658 | 3.0 | 24 |

**cpp-medium-01-cycle-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2978 | 3.0 | 51 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2948 | 3.0 | 44 |
| `glm-5.1:cloud` | PASS | 2 | 2446 | 3.0 | 48 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 5206 | 3.0 | 54 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2534 | 3.0 | 47 |

**csharp-medium-01-trapped-rainwater**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2230 | 4.0 | 26 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2227 | 4.0 | 25 |
| `glm-5.1:cloud` | tests-fail | 2 | 1885 | 4.0 | 35 |
| `kimi-k2.6:cloud` | PASS | 2 | 2714 | 4.0 | 38 |
| `minimax-m2.7:cloud` | PASS | 2 | 2276 | 4.0 | 32 |

**go-medium-01-koko-bananas**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2291 | 4.0 | 31 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2412 | 3.0 | 39 |
| `glm-5.1:cloud` | PASS | 2 | 2599 | 4.0 | 33 |
| `kimi-k2.6:cloud` | PASS | 2 | 3530 | 4.0 | 36 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2721 | 4.0 | 44 |

**python-medium-01-meeting-rooms**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2363 | 4.0 | 17 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2452 | 4.0 | 20 |
| `glm-5.1:cloud` | tests-fail | 2 | 1713 | 5.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 2662 | 5.0 | 22 |
| `minimax-m2.7:cloud` | PASS | 2 | 1722 | 5.0 | 11 |

**rust-medium-01-search-rotated**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2647 | 3.0 | 40 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2603 | 3.0 | 33 |
| `glm-5.1:cloud` | PASS | 2 | 2028 | 3.0 | 34 |
| `kimi-k2.6:cloud` | PASS | 2 | 4357 | 4.0 | 34 |
| `minimax-m2.7:cloud` | PASS | 2 | 2331 | 4.0 | 32 |

### hard

**c-hard-01-quicksort-3way**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3202 | 4.0 | 53 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 3221 | 4.0 | 47 |
| `glm-5.1:cloud` | tests-fail | 2 | 2866 | 4.0 | 63 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 8323 | 4.0 | 57 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3380 | 4.0 | 59 |

**cpp-hard-01-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3140 | 3.0 | 76 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 3307 | 1.0 | 79 |
| `glm-5.1:cloud` | tests-fail | 2 | 2789 | 3.0 | 75 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 3324 | 4.0 | 69 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3329 | 2.0 | 114 |

**csharp-hard-01-async-debounce**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2727 | 2.0 | 73 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2702 | 2.0 | 64 |
| `glm-5.1:cloud` | tests-fail | 2 | 19257 | 2.0 | 101 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 11188 | 3.0 | 120 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2994 | 1.0 | 74 |

**go-hard-01-shortest-path-k-stops**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2902 | 4.0 | 55 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2832 | 1.0 | 50 |
| `glm-5.1:cloud` | PASS | 2 | 5456 | 4.0 | 76 |
| `kimi-k2.6:cloud` | PASS | 2 | 4755 | 4.0 | 53 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3329 | 2.0 | 47 |

**python-hard-01-lru-cache**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2857 | 5.0 | 51 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2691 | 4.0 | 35 |
| `glm-5.1:cloud` | tests-fail | 2 | 2433 | 4.0 | 33 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 4257 | 3.0 | 53 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2470 | 3.0 | 24 |

**rust-hard-01-iter-window-pairs**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3099 | 4.0 | 63 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3087 | 3.0 | 53 |
| `glm-5.1:cloud` | tests-fail | 2 | 2208 | 3.0 | 48 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 3888 | 1.0 | 36 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3633 | 1.0 | 58 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models kimi-k2.6:cloud,glm-5.1:cloud,deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`861046693fa9` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
