# Skill-Eval Report — coder_mlang suite v1.0.1

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_mlang` |
| suite_version | `1.0.1` |
| suite_hash | `ddef8095311c258d...` |
| released | `2026-05-17` |
| run_started_at | `2026-10-22T07:19:41.498835Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.161:11434` |
| judge_model | `(no judge)` |
| git_commit | `8f69cee` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | 18 | 33% (6/18) | 100% (18/18) | 39.9s | 9799 | 3.11 | ❌ |

## Recommended default for `cfg.roles.coder.model`

no model meets the rubric (pass_rate ≥ 70%, quality ≥ 3.5). Best so far: **minimax-m3.1-flash:cloud** with pass_rate=33%, avg_quality=3.11. The role's default stays at the project-global DEFAULT_MODEL until a follow-up run produces a qualifying candidate.

## Per-question detail

### medium

**c-medium-01-longest-valid-parens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3074 | 5.0 | 42 |

**cpp-medium-01-cycle-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 4 | 13349 | 3.0 | 86 |

**csharp-medium-01-trapped-rainwater**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2895 | 4.0 | 49 |

**go-medium-01-koko-bananas**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3360 | 3.0 | 82 |

**python-medium-01-meeting-rooms**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2762 | 5.0 | 29 |

**rust-medium-01-search-rotated**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 15768 | 2.0 | 80 |

### hard

**c-hard-01-quicksort-3way**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3821 | 2.0 | 60 |

**cpp-hard-01-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3921 | 4.0 | 108 |

**csharp-hard-01-async-debounce**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 4 | 33154 | 1.0 | 219 |

**go-hard-01-shortest-path-k-stops**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 7950 | 4.0 | 122 |

**python-hard-01-lru-cache**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3594 | 5.0 | 64 |

**rust-hard-01-iter-window-pairs**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 7029 | 1.0 | 102 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models minimax-m3.1-flash:cloud \
    --ollama-base http://192.168.178.161:11434 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`ddef8095311c` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
