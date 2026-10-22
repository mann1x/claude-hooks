# Skill-Eval Report — coder_mlang suite v1.0.1

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_mlang` |
| suite_version | `1.0.1` |
| suite_hash | `ddef8095311c258d...` |
| released | `2026-05-17` |
| run_started_at | `2026-10-22T07:53:16.712620Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.161:11434` |
| judge_model | `(no judge)` |
| git_commit | `8f69cee` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | 18 | 39% (7/18) | 100% (18/18) | 30.1s | 7013 | 4.00 | ❌ |

## Recommended default for `cfg.roles.coder.model`

no model meets the rubric (pass_rate ≥ 70%, quality ≥ 3.5). Best so far: **minimax-m3.1-flash:cloud** with pass_rate=39%, avg_quality=4.00. The role's default stays at the project-global DEFAULT_MODEL until a follow-up run produces a qualifying candidate.

## Per-question detail

### medium

**c-medium-01-longest-valid-parens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5913 | 4.0 | 37 |

**cpp-medium-01-cycle-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 3 | 25489 | 3.0 | 165 |

**csharp-medium-01-trapped-rainwater**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3641 | 5.0 | 38 |

**go-medium-01-koko-bananas**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5361 | 5.0 | 43 |

**python-medium-01-meeting-rooms**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 3 | 5050 | 5.0 | 32 |

**rust-medium-01-search-rotated**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3700 | 4.0 | 40 |

### hard

**c-hard-01-quicksort-3way**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 5042 | 4.0 | 80 |

**cpp-hard-01-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 5617 | 4.0 | 115 |

**csharp-hard-01-async-debounce**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 8399 | 2.0 | 145 |

**go-hard-01-shortest-path-k-stops**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 12409 | 4.0 | 141 |

**python-hard-01-lru-cache**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 4287 | 4.0 | 83 |

**rust-hard-01-iter-window-pairs**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4020 | 4.0 | 66 |

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
