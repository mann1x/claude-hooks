# Skill-Eval Report — coder_med suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_med` |
| suite_version | `1.0` |
| suite_hash | `0e6ab0fd6b955807...` |
| released | `2026-06-04` |
| run_started_at | `2026-10-22T06:01:49.459820Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.161:11434` |
| judge_model | `(no judge)` |
| git_commit | `6df99ad` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | 60 | 90% (54/60) | 100% (60/60) | 5.3s | 3345 | 4.27 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**minimax-m3.1-flash:cloud** wins the rubric: pass_rate=90%, avg_quality=4.27, median_tokens=3345.

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### medium

**c-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2921 | 5.0 | 17 |

**c-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3089 | 4.0 | 30 |

**c-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2994 | 1.0 | 24 |

**c-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 4144 | 1.0 | 45 |

**c-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3345 | 4.0 | 35 |

**c-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4412 | 4.0 | 44 |

**c-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2879 | 5.0 | 11 |

**c-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 4720 | 1.0 | 94 |

**c-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3651 | 4.0 | 35 |

**c-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4719 | 5.0 | 34 |

**cpp-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2832 | 5.0 | 14 |

**cpp-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2998 | 5.0 | 25 |

**cpp-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3038 | 5.0 | 26 |

**cpp-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3579 | 5.0 | 31 |

**cpp-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3802 | 1.0 | 37 |

**cpp-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3693 | 5.0 | 33 |

**cpp-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2939 | 5.0 | 15 |

**cpp-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3029 | 5.0 | 22 |

**cpp-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4041 | 5.0 | 49 |

**cpp-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3346 | 5.0 | 25 |

**csharp-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2886 | 5.0 | 23 |

**csharp-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3025 | 5.0 | 37 |

**csharp-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3016 | 5.0 | 34 |

**csharp-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4021 | 5.0 | 40 |

**csharp-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3319 | 5.0 | 39 |

**csharp-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3850 | 5.0 | 39 |

**csharp-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3166 | 4.0 | 31 |

**csharp-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2999 | 5.0 | 29 |

**csharp-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3528 | 4.0 | 46 |

**csharp-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3465 | 1.0 | 34 |

**go-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2963 | 5.0 | 32 |

**go-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3066 | 4.0 | 37 |

**go-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3120 | 4.0 | 39 |

**go-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 7123 | 5.0 | 51 |

**go-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3617 | 5.0 | 46 |

**go-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4145 | 4.0 | 85 |

**go-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3418 | 3.0 | 56 |

**go-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5118 | 5.0 | 29 |

**go-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3952 | 4.0 | 46 |

**go-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3887 | 5.0 | 49 |

**python-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2770 | 5.0 | 12 |

**python-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2939 | 5.0 | 19 |

**python-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2934 | 5.0 | 16 |

**python-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3731 | 5.0 | 30 |

**python-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3787 | 4.0 | 37 |

**python-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3592 | 5.0 | 29 |

**python-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2842 | 5.0 | 14 |

**python-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2794 | 5.0 | 11 |

**python-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3040 | 4.0 | 22 |

**python-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3361 | 5.0 | 21 |

**rust-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2961 | 5.0 | 21 |

**rust-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2979 | 5.0 | 35 |

**rust-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3083 | 5.0 | 31 |

**rust-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3877 | 4.0 | 55 |

**rust-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3911 | 4.0 | 42 |

**rust-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 5014 | 5.0 | 42 |

**rust-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3103 | 1.0 | 25 |

**rust-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2975 | 5.0 | 21 |

**rust-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 4188 | 1.0 | 61 |

**rust-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3739 | 5.0 | 37 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models minimax-m3.1-flash:cloud \
    --ollama-base http://192.168.178.161:11434 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`0e6ab0fd6b95` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
