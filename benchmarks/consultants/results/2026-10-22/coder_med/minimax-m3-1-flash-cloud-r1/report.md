# Skill-Eval Report — coder_med suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_med` |
| suite_version | `1.0` |
| suite_hash | `0e6ab0fd6b955807...` |
| released | `2026-06-04` |
| run_started_at | `2026-10-22T05:34:04.058868Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.161:11434` |
| judge_model | `(no judge)` |
| git_commit | `4f6afa7` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | 60 | 93% (56/60) | 100% (60/60) | 5.0s | 3362 | 4.40 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**minimax-m3.1-flash:cloud** wins the rubric: pass_rate=93%, avg_quality=4.40, median_tokens=3362.

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### medium

**c-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3304 | 5.0 | 23 |

**c-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3150 | 5.0 | 29 |

**c-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3130 | 5.0 | 32 |

**c-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4202 | 4.0 | 40 |

**c-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4429 | 5.0 | 27 |

**c-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3798 | 5.0 | 37 |

**c-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2927 | 5.0 | 16 |

**c-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 4380 | 1.0 | 68 |

**c-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 5776 | 4.0 | 47 |

**c-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 8842 | 5.0 | 37 |

**cpp-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2852 | 5.0 | 17 |

**cpp-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3098 | 5.0 | 19 |

**cpp-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3001 | 5.0 | 27 |

**cpp-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3550 | 5.0 | 31 |

**cpp-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3392 | 5.0 | 34 |

**cpp-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3748 | 4.0 | 45 |

**cpp-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3101 | 1.0 | 14 |

**cpp-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3080 | 5.0 | 23 |

**cpp-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3982 | 4.0 | 41 |

**cpp-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3920 | 5.0 | 26 |

**csharp-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2811 | 5.0 | 21 |

**csharp-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3226 | 5.0 | 40 |

**csharp-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3045 | 5.0 | 34 |

**csharp-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4163 | 4.0 | 55 |

**csharp-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3379 | 5.0 | 43 |

**csharp-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3852 | 4.0 | 39 |

**csharp-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2971 | 5.0 | 29 |

**csharp-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3297 | 4.0 | 34 |

**csharp-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3507 | 4.0 | 47 |

**csharp-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3619 | 4.0 | 40 |

**go-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2870 | 4.0 | 26 |

**go-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2987 | 5.0 | 31 |

**go-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3059 | 5.0 | 44 |

**go-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4761 | 3.0 | 116 |

**go-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3345 | 4.0 | 63 |

**go-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 5055 | 5.0 | 55 |

**go-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3020 | 5.0 | 28 |

**go-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5039 | 4.0 | 25 |

**go-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 6568 | 4.0 | 58 |

**go-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3646 | 1.0 | 51 |

**python-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2793 | 5.0 | 21 |

**python-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2774 | 5.0 | 16 |

**python-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2874 | 5.0 | 14 |

**python-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3553 | 5.0 | 28 |

**python-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3487 | 5.0 | 22 |

**python-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3628 | 5.0 | 29 |

**python-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2819 | 5.0 | 14 |

**python-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2717 | 5.0 | 11 |

**python-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3158 | 4.0 | 24 |

**python-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3210 | 5.0 | 21 |

**rust-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2914 | 4.0 | 25 |

**rust-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2992 | 5.0 | 28 |

**rust-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3085 | 5.0 | 30 |

**rust-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 6615 | 4.0 | 39 |

**rust-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3698 | 4.0 | 37 |

**rust-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4635 | 4.0 | 63 |

**rust-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2984 | 5.0 | 21 |

**rust-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3479 | 5.0 | 26 |

**rust-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 6742 | 1.0 | 87 |

**rust-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4279 | 5.0 | 33 |

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
