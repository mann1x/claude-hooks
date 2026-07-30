# Skill-Eval Report — coder_mlang suite v1.0.1

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_mlang` |
| suite_version | `1.0.1` |
| suite_hash | `ddef8095311c258d...` |
| released | `2026-05-17` |
| run_started_at | `2026-06-03T22:25:31.584410Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `d0fc724` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3:cloud` | 13 | 31% (4/13) | 100% (13/13) | 45.3s | 6546 | 3.40 | ❌ |
| `nemotron-3-super:cloud` | 13 | 23% (3/13) | 92% (12/13) | 10.5s | 3939 | 2.55 | ❌ |

## Recommended default for `cfg.roles.coder.model`

no model meets the rubric (pass_rate ≥ 70%, quality ≥ 3.5). Best so far: **minimax-m3:cloud** with pass_rate=31%, avg_quality=3.40. The role's default stays at the project-global DEFAULT_MODEL until a follow-up run produces a qualifying candidate.

## Per-question detail

### medium

**cpp-medium-01-cycle-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 4109 | 4.0 | 54 |
| `nemotron-3-super:cloud` | PASS | 2 | 3329 | 3.0 | 57 |

**csharp-medium-01-trapped-rainwater**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2647 | 4.0 | 44 |
| `nemotron-3-super:cloud` | PASS | 2 | 2790 | 4.0 | 37 |

### hard

**c-hard-01-quicksort-3way**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 4473 | — | 82 |
| `nemotron-3-super:cloud` | PASS | 2 | 3939 | — | 49 |

**cpp-hard-01-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | tests-fail | 2 | 3876 | 3.0 | 104 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 3430 | 2.0 | 83 |

**csharp-hard-01-async-debounce**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | tests-fail | 2 | 17835 | 3.0 | 184 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 4189 | 1.0 | 76 |

**python-hard-01-lru-cache**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | tests-fail | 2 | 4093 | 3.0 | 88 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2611 | 4.0 | 18 |

**rust-hard-01-iter-window-pairs**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 6546 | 4.0 | 58 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 3875 | 3.0 | 61 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models minimax-m3:cloud,nemotron-3-super:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`ddef8095311c` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
