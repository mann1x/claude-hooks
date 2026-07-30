# Skill-Eval Report — coder_med suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_med` |
| suite_version | `1.0` |
| suite_hash | `0e6ab0fd6b955807...` |
| released | `2026-06-04` |
| run_started_at | `2026-06-04T06:40:57.968379Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `5f5af00` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `kimi-k2.6:cloud` | 60 | 100% (60/60) | 100% (60/60) | 48.5s | 3069 | 4.19 | ✅ |
| `deepseek-v4-pro:cloud` | 60 | 98% (59/60) | 100% (60/60) | 53.8s | 2964 | 4.00 | ✅ |
| `minimax-m3:cloud` | 60 | 97% (58/60) | 100% (60/60) | 59.7s | 3581 | 3.92 | ✅ |
| `glm-5.1:cloud` | 60 | 95% (57/60) | 100% (60/60) | 41.0s | 2529 | 3.85 | ✅ |
| `deepseek-v4-flash:cloud` | 60 | 92% (55/60) | 100% (60/60) | 46.8s | 2896 | 3.96 | ✅ |
| `nemotron-3-super:cloud` | 60 | 88% (53/60) | 100% (60/60) | 39.0s | 3413 | 3.62 | ✅ |
| `minimax-m2.7:cloud` | 60 | 77% (46/60) | 100% (60/60) | 52.3s | 2766 | 3.30 | ❌ |

## Recommended default for `cfg.roles.coder.model`

**kimi-k2.6:cloud** wins the rubric: pass_rate=100%, avg_quality=4.19, median_tokens=3069. Also qualifying: deepseek-v4-pro:cloud (98%/4.00), minimax-m3:cloud (97%/3.92), glm-5.1:cloud (95%/3.85), deepseek-v4-flash:cloud (92%/3.96), nemotron-3-super:cloud (88%/3.62).

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### medium

**c-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2598 | 5.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2709 | 5.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2167 | — | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 2827 | 5.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 2148 | 5.0 | 16 |
| `minimax-m3:cloud` | PASS | 2 | 2804 | 5.0 | 20 |
| `nemotron-3-super:cloud` | PASS | 2 | 2977 | 4.0 | 19 |

**c-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2785 | 4.0 | 26 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2978 | 4.0 | 28 |
| `glm-5.1:cloud` | PASS | 2 | 2251 | — | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 3131 | 4.0 | 24 |
| `minimax-m2.7:cloud` | PASS | 2 | 3008 | 4.0 | 34 |
| `minimax-m3:cloud` | PASS | 2 | 2994 | — | 23 |
| `nemotron-3-super:cloud` | PASS | 2 | 3286 | 4.0 | 50 |

**c-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2867 | — | 30 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2844 | — | 24 |
| `glm-5.1:cloud` | PASS | 2 | 2407 | — | 26 |
| `kimi-k2.6:cloud` | PASS | 2 | 3592 | — | 24 |
| `minimax-m2.7:cloud` | PASS | 2 | 2327 | — | 28 |
| `minimax-m3:cloud` | PASS | 2 | 4149 | 5.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 3477 | 5.0 | 28 |

**c-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3627 | — | 33 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3801 | — | 42 |
| `glm-5.1:cloud` | PASS | 2 | 5733 | 4.0 | 32 |
| `kimi-k2.6:cloud` | PASS | 2 | 5945 | — | 51 |
| `minimax-m2.7:cloud` | PASS | 2 | 3549 | — | 28 |
| `minimax-m3:cloud` | PASS | 2 | 6679 | — | 44 |
| `nemotron-3-super:cloud` | PASS | 2 | 6228 | — | 71 |

**c-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3920 | 1.0 | 29 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3524 | 3.0 | 47 |
| `glm-5.1:cloud` | PASS | 2 | 3535 | — | 25 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3972 | 4.0 | 36 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 4754 | 1.0 | 21 |
| `minimax-m3:cloud` | PASS | 2 | 4022 | 5.0 | 39 |
| `nemotron-3-super:cloud` | PASS | 2 | 4030 | — | 64 |

**c-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3551 | 4.0 | 41 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3693 | 4.0 | 43 |
| `glm-5.1:cloud` | PASS | 2 | 3029 | 4.0 | 25 |
| `kimi-k2.6:cloud` | PASS | 2 | 6000 | 4.0 | 36 |
| `minimax-m2.7:cloud` | PASS | 2 | 3411 | 4.0 | 41 |
| `minimax-m3:cloud` | PASS | 2 | 4888 | 4.0 | 36 |
| `nemotron-3-super:cloud` | PASS | 2 | 4077 | — | 41 |

**c-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2893 | 4.0 | 25 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2889 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 2266 | — | 16 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3014 | 5.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 2252 | 5.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 3537 | 5.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 3474 | 4.0 | 21 |

**c-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3847 | — | 44 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 7175 | 3.0 | 68 |
| `glm-5.1:cloud` | PASS | 2 | 3272 | — | 34 |
| `kimi-k2.6:cloud` | PASS | 2 | 7682 | — | 34 |
| `minimax-m2.7:cloud` | PASS | 2 | 3157 | — | 59 |
| `minimax-m3:cloud` | PASS | 2 | 8997 | — | 84 |
| `nemotron-3-super:cloud` | PASS | 2 | 4454 | — | 49 |

**c-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3349 | — | 42 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3259 | 4.0 | 32 |
| `glm-5.1:cloud` | PASS | 2 | 2589 | 3.0 | 24 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3580 | 4.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 3657 | 2.0 | 40 |
| `minimax-m3:cloud` | PASS | 2 | 4058 | — | 32 |
| `nemotron-3-super:cloud` | PASS | 3 | 6237 | 5.0 | 24 |

**c-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3479 | — | 40 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 5109 | 4.0 | 35 |
| `glm-5.1:cloud` | PASS | 2 | 2875 | 4.0 | 30 |
| `kimi-k2.6:cloud` | PASS | 2 | 4615 | 5.0 | 32 |
| `minimax-m2.7:cloud` | PASS | 2 | 2718 | — | 22 |
| `minimax-m3:cloud` | PASS | 2 | 10271 | — | 39 |
| `nemotron-3-super:cloud` | PASS | 2 | 4003 | — | 37 |

**cpp-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2635 | 5.0 | 18 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2697 | 4.0 | 18 |
| `glm-5.1:cloud` | PASS | 2 | 2143 | 5.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2283 | 4.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2236 | 3.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 2938 | 5.0 | 23 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2904 | 1.0 | 19 |

**cpp-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2728 | 5.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2826 | 5.0 | 25 |
| `glm-5.1:cloud` | PASS | 2 | 2284 | 4.0 | 20 |
| `kimi-k2.6:cloud` | PASS | 2 | 2471 | 4.0 | 25 |
| `minimax-m2.7:cloud` | PASS | 2 | 2415 | 4.0 | 28 |
| `minimax-m3:cloud` | PASS | 2 | 2989 | 5.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 3103 | 3.0 | 24 |

**cpp-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2759 | 5.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2945 | 5.0 | 27 |
| `glm-5.1:cloud` | PASS | 2 | 2330 | 3.0 | 18 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2689 | 4.0 | 30 |
| `minimax-m2.7:cloud` | PASS | 2 | 2463 | 4.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 1515 | 3.0 | 17 |
| `nemotron-3-super:cloud` | PASS | 2 | 3166 | 5.0 | 27 |

**cpp-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3213 | 4.0 | 18 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3913 | 5.0 | 22 |
| `glm-5.1:cloud` | tests-fail | 2 | 2772 | — | 20 |
| `kimi-k2.6:cloud` | PASS | 2 | 3465 | 5.0 | 28 |
| `minimax-m2.7:cloud` | PASS | 2 | 3511 | — | 28 |
| `minimax-m3:cloud` | PASS | 2 | 3572 | — | 25 |
| `nemotron-3-super:cloud` | PASS | 2 | 3778 | 4.0 | 36 |

**cpp-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 5301 | 4.0 | 27 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3315 | 4.0 | 42 |
| `glm-5.1:cloud` | PASS | 2 | 2791 | 4.0 | 41 |
| `kimi-k2.6:cloud` | PASS | 2 | 3735 | 5.0 | 35 |
| `minimax-m2.7:cloud` | tests-fail | 4 | 10890 | 1.0 | 32 |
| `minimax-m3:cloud` | PASS | 2 | 5491 | — | 36 |
| `nemotron-3-super:cloud` | PASS | 2 | 5813 | — | 31 |

**cpp-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3490 | 5.0 | 34 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3656 | 4.0 | 44 |
| `glm-5.1:cloud` | PASS | 2 | 2967 | 4.0 | 27 |
| `kimi-k2.6:cloud` | PASS | 2 | 5417 | 4.0 | 44 |
| `minimax-m2.7:cloud` | PASS | 2 | 4637 | 4.0 | 43 |
| `minimax-m3:cloud` | PASS | 2 | 3715 | — | 30 |
| `nemotron-3-super:cloud` | PASS | 2 | 3877 | 4.0 | 35 |

**cpp-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2805 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2724 | 4.0 | 14 |
| `glm-5.1:cloud` | PASS | 2 | 2241 | 4.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 2594 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2425 | 4.0 | 15 |
| `minimax-m3:cloud` | PASS | 2 | 3006 | 4.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2996 | 4.0 | 19 |

**cpp-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2666 | 5.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2780 | 5.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2208 | 3.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 2577 | 5.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 2341 | — | 18 |
| `minimax-m3:cloud` | PASS | 2 | 2940 | 4.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 3227 | 5.0 | 18 |

**cpp-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3080 | 4.0 | 35 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3091 | — | 30 |
| `glm-5.1:cloud` | PASS | 2 | 2529 | 4.0 | 24 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2882 | — | 26 |
| `minimax-m2.7:cloud` | PASS | 2 | 3017 | 4.0 | 30 |
| `minimax-m3:cloud` | PASS | 2 | 6756 | 3.0 | 57 |
| `nemotron-3-super:cloud` | PASS | 2 | 3580 | 4.0 | 34 |

**cpp-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3173 | 5.0 | 30 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3170 | 5.0 | 20 |
| `glm-5.1:cloud` | PASS | 2 | 2589 | 4.0 | 20 |
| `kimi-k2.6:cloud` | PASS | 2 | 4412 | 5.0 | 22 |
| `minimax-m2.7:cloud` | PASS | 2 | 2729 | 5.0 | 26 |
| `minimax-m3:cloud` | PASS | 2 | 3255 | 4.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 3353 | 4.0 | 28 |

**csharp-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2580 | 4.0 | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2675 | 4.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2202 | 5.0 | 21 |
| `kimi-k2.6:cloud` | PASS | 2 | 2428 | 5.0 | 30 |
| `minimax-m2.7:cloud` | PASS | 2 | 2136 | 3.0 | 18 |
| `minimax-m3:cloud` | PASS | 2 | 2745 | 4.0 | 20 |
| `nemotron-3-super:cloud` | PASS | 2 | 2742 | 4.0 | 26 |

**csharp-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2693 | 5.0 | 33 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2748 | 4.0 | 32 |
| `glm-5.1:cloud` | PASS | 2 | 2258 | 5.0 | 22 |
| `kimi-k2.6:cloud` | PASS | 2 | 2393 | 5.0 | 26 |
| `minimax-m2.7:cloud` | PASS | 2 | 2450 | 3.0 | 32 |
| `minimax-m3:cloud` | PASS | 2 | 3080 | 5.0 | 32 |
| `nemotron-3-super:cloud` | PASS | 2 | 2893 | 5.0 | 33 |

**csharp-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2740 | 5.0 | 24 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2752 | 5.0 | 26 |
| `glm-5.1:cloud` | PASS | 2 | 2306 | 5.0 | 24 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2515 | 4.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 2268 | 5.0 | 29 |
| `minimax-m3:cloud` | PASS | 2 | 3127 | 4.0 | 38 |
| `nemotron-3-super:cloud` | PASS | 2 | 3112 | 5.0 | 30 |

**csharp-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3344 | 1.0 | 25 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3391 | 5.0 | 28 |
| `glm-5.1:cloud` | PASS | 3 | 7050 | 3.0 | 27 |
| `kimi-k2.6:cloud` | PASS | 2 | 5218 | 4.0 | 44 |
| `minimax-m2.7:cloud` | tests-fail | 3 | 11145 | 1.0 | 25 |
| `minimax-m3:cloud` | PASS | 2 | 4669 | 2.0 | 43 |
| `nemotron-3-super:cloud` | PASS | 2 | 4338 | 4.0 | 44 |

**csharp-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 4013 | 4.0 | 38 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3699 | — | 45 |
| `glm-5.1:cloud` | PASS | 2 | 2780 | — | 53 |
| `kimi-k2.6:cloud` | PASS | 2 | 3124 | 4.0 | 48 |
| `minimax-m2.7:cloud` | PASS | 2 | 3264 | 4.0 | 44 |
| `minimax-m3:cloud` | PASS | 2 | 5265 | 4.0 | 57 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 4496 | 1.0 | 44 |

**csharp-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3440 | — | 32 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3624 | 4.0 | 38 |
| `glm-5.1:cloud` | PASS | 2 | 2995 | — | 31 |
| `kimi-k2.6:cloud` | PASS | 2 | 6337 | — | 44 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3251 | 1.0 | 36 |
| `minimax-m3:cloud` | PASS | 2 | 5399 | 4.0 | 51 |
| `nemotron-3-super:cloud` | PASS | 2 | 4115 | — | 63 |

**csharp-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2749 | 4.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2763 | 3.0 | 15 |
| `glm-5.1:cloud` | PASS | 2 | 2238 | 5.0 | 19 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3433 | 4.0 | 20 |
| `minimax-m2.7:cloud` | PASS | 2 | 2749 | 2.0 | 19 |
| `minimax-m3:cloud` | PASS | 2 | 2940 | 4.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 3009 | — | 19 |

**csharp-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2686 | — | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2704 | 3.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2251 | 3.0 | 23 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3386 | 4.0 | 25 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2530 | 3.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 2935 | — | 23 |
| `nemotron-3-super:cloud` | PASS | 2 | 3243 | 3.0 | 29 |

**csharp-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2956 | — | 27 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2811 | 2.0 | 14 |
| `glm-5.1:cloud` | PASS | 2 | 2632 | — | 32 |
| `kimi-k2.6:cloud` | PASS | 2 | 3810 | 3.0 | 35 |
| `minimax-m2.7:cloud` | PASS | 2 | 3625 | 3.0 | 48 |
| `minimax-m3:cloud` | PASS | 2 | 4537 | 3.0 | 43 |
| `nemotron-3-super:cloud` | PASS | 2 | 3615 | — | 36 |

**csharp-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3186 | — | 35 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 4106 | — | 24 |
| `glm-5.1:cloud` | PASS | 2 | 2694 | 4.0 | 25 |
| `kimi-k2.6:cloud` | ERROR | 2 | 4341 | 4.0 | 26 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2687 | 2.0 | 23 |
| `minimax-m3:cloud` | PASS | 2 | 3965 | 4.0 | 41 |
| `nemotron-3-super:cloud` | PASS | 2 | 3659 | 4.0 | 32 |

**go-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2743 | 3.0 | 29 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2817 | 4.0 | 29 |
| `glm-5.1:cloud` | PASS | 2 | 2327 | 3.0 | 34 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2849 | 4.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 2170 | 3.0 | 25 |
| `minimax-m3:cloud` | tests-fail | 2 | 3241 | 1.0 | 48 |
| `nemotron-3-super:cloud` | PASS | 2 | 2796 | 3.0 | 30 |

**go-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2775 | 4.0 | 31 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2842 | — | 32 |
| `glm-5.1:cloud` | PASS | 2 | 2351 | 4.0 | 31 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2400 | 4.0 | 31 |
| `minimax-m2.7:cloud` | PASS | 2 | 2783 | — | 30 |
| `minimax-m3:cloud` | PASS | 2 | 3418 | 3.0 | 37 |
| `nemotron-3-super:cloud` | PASS | 2 | 3201 | 3.0 | 32 |

**go-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2777 | 4.0 | 26 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2780 | 4.0 | 32 |
| `glm-5.1:cloud` | PASS | 2 | 2402 | 4.0 | 24 |
| `kimi-k2.6:cloud` | PASS | 2 | 2625 | 3.0 | 40 |
| `minimax-m2.7:cloud` | PASS | 2 | 2322 | 4.0 | 34 |
| `minimax-m3:cloud` | PASS | 2 | 2966 | 4.0 | 29 |
| `nemotron-3-super:cloud` | PASS | 2 | 3137 | 4.0 | 34 |

**go-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3632 | 2.0 | 59 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3811 | 4.0 | 63 |
| `glm-5.1:cloud` | PASS | 3 | 6511 | 4.0 | 33 |
| `kimi-k2.6:cloud` | PASS | 2 | 3829 | 4.0 | 53 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3753 | 1.0 | 47 |
| `minimax-m3:cloud` | PASS | 6 | 26374 | 3.0 | 89 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 4143 | — | 57 |

**go-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 4059 | 4.0 | 43 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 4049 | 4.0 | 47 |
| `glm-5.1:cloud` | PASS | 2 | 2857 | 3.0 | 48 |
| `kimi-k2.6:cloud` | ERROR | 2 | 4050 | — | 50 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3260 | 1.0 | 51 |
| `minimax-m3:cloud` | PASS | 2 | 4622 | 4.0 | 54 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 4572 | 1.0 | 66 |

**go-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3367 | 4.0 | 36 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3706 | 4.0 | 51 |
| `glm-5.1:cloud` | PASS | 2 | 3032 | 4.0 | 46 |
| `kimi-k2.6:cloud` | PASS | 2 | 5350 | 4.0 | 52 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3754 | 1.0 | 55 |
| `minimax-m3:cloud` | PASS | 2 | 3782 | 4.0 | 54 |
| `nemotron-3-super:cloud` | PASS | 2 | 4372 | 3.0 | 69 |

**go-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2879 | 3.0 | 35 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2883 | 3.0 | 32 |
| `glm-5.1:cloud` | PASS | 2 | 2323 | 3.0 | 27 |
| `kimi-k2.6:cloud` | PASS | 2 | 2538 | 3.0 | 33 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2403 | 1.0 | 28 |
| `minimax-m3:cloud` | PASS | 2 | 3246 | 3.0 | 44 |
| `nemotron-3-super:cloud` | PASS | 2 | 3347 | 3.0 | 43 |

**go-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2674 | 4.0 | 24 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2684 | 4.0 | 23 |
| `glm-5.1:cloud` | PASS | 2 | 2196 | 4.0 | 23 |
| `kimi-k2.6:cloud` | PASS | 2 | 2927 | 3.0 | 23 |
| `minimax-m2.7:cloud` | PASS | 2 | 3045 | 4.0 | 23 |
| `minimax-m3:cloud` | PASS | 2 | 4004 | 4.0 | 25 |
| `nemotron-3-super:cloud` | PASS | 2 | 3104 | 5.0 | 28 |

**go-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2809 | 5.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3232 | 3.0 | 47 |
| `glm-5.1:cloud` | PASS | 2 | 2259 | 3.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 2764 | 4.0 | 16 |
| `minimax-m2.7:cloud` | PASS | 2 | 3743 | 3.0 | 63 |
| `minimax-m3:cloud` | PASS | 2 | 4063 | 2.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 3505 | 3.0 | 30 |

**go-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3224 | 4.0 | 36 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3294 | 3.0 | 37 |
| `glm-5.1:cloud` | PASS | 2 | 2751 | — | 39 |
| `kimi-k2.6:cloud` | PASS | 2 | 4564 | 5.0 | 41 |
| `minimax-m2.7:cloud` | PASS | 2 | 2964 | 4.0 | 42 |
| `minimax-m3:cloud` | PASS | 2 | 8134 | 4.0 | 46 |
| `nemotron-3-super:cloud` | PASS | 2 | 3541 | 4.0 | 58 |

**python-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2574 | 5.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2591 | 5.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2121 | 4.0 | 17 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2240 | 4.0 | 19 |
| `minimax-m2.7:cloud` | PASS | 2 | 2088 | 5.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 2663 | 4.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2876 | 4.0 | 18 |

**python-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2608 | 5.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2604 | 5.0 | 16 |
| `glm-5.1:cloud` | PASS | 2 | 2133 | 4.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 2554 | 5.0 | 18 |
| `minimax-m2.7:cloud` | PASS | 2 | 1952 | 4.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 1407 | 5.0 | 16 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2865 | 1.0 | 15 |

**python-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2670 | 5.0 | 18 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2707 | 5.0 | 18 |
| `glm-5.1:cloud` | PASS | 2 | 2224 | 5.0 | 11 |
| `kimi-k2.6:cloud` | PASS | 2 | 2449 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2124 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 2812 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 3153 | 4.0 | 19 |

**python-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3109 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3753 | 4.0 | 22 |
| `glm-5.1:cloud` | PASS | 2 | 3526 | 5.0 | 15 |
| `kimi-k2.6:cloud` | PASS | 2 | 4869 | 4.0 | 26 |
| `minimax-m2.7:cloud` | PASS | 2 | 3449 | 5.0 | 16 |
| `minimax-m3:cloud` | tests-fail | 2 | 3328 | 2.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 4728 | 4.0 | 33 |

**python-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3799 | 3.0 | 42 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2951 | 5.0 | 25 |
| `glm-5.1:cloud` | PASS | 2 | 2566 | 3.0 | 25 |
| `kimi-k2.6:cloud` | PASS | 2 | 4584 | 3.0 | 38 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3724 | 1.0 | 21 |
| `minimax-m3:cloud` | PASS | 3 | 6014 | — | 38 |
| `nemotron-3-super:cloud` | PASS | 3 | 6245 | 3.0 | 48 |

**python-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3394 | 4.0 | 31 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3564 | 4.0 | 36 |
| `glm-5.1:cloud` | PASS | 2 | 2902 | 5.0 | 28 |
| `kimi-k2.6:cloud` | PASS | 2 | 4843 | 4.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 3129 | 4.0 | 26 |
| `minimax-m3:cloud` | PASS | 2 | 3590 | 4.0 | 30 |
| `nemotron-3-super:cloud` | PASS | 2 | 3593 | 5.0 | 33 |

**python-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2665 | 5.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2555 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 2157 | 5.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2186 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2387 | 5.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 2747 | 5.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2889 | 5.0 | 13 |

**python-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2515 | 4.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2550 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 3 | 3352 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 2021 | 5.0 | 5 |
| `minimax-m2.7:cloud` | PASS | 2 | 2193 | 5.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 3020 | 5.0 | 6 |
| `nemotron-3-super:cloud` | PASS | 2 | 2662 | 4.0 | 14 |

**python-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2865 | 5.0 | 21 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2997 | 1.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2300 | 5.0 | 15 |
| `kimi-k2.6:cloud` | PASS | 2 | 2557 | 5.0 | 19 |
| `minimax-m2.7:cloud` | PASS | 2 | 2945 | 3.0 | 25 |
| `minimax-m3:cloud` | PASS | 2 | 3041 | 4.0 | 21 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 3236 | 1.0 | 31 |

**python-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2971 | 5.0 | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3187 | 5.0 | 28 |
| `glm-5.1:cloud` | PASS | 2 | 2530 | 5.0 | 21 |
| `kimi-k2.6:cloud` | PASS | 2 | 2829 | 5.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 2679 | 5.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 3760 | 5.0 | 24 |
| `nemotron-3-super:cloud` | PASS | 2 | 3337 | 5.0 | 24 |

**rust-med-01-rle-encode**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2664 | 3.0 | 24 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2652 | 3.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2208 | 4.0 | 17 |
| `kimi-k2.6:cloud` | PASS | 2 | 2496 | 4.0 | 25 |
| `minimax-m2.7:cloud` | PASS | 2 | 2704 | 3.0 | 25 |
| `minimax-m3:cloud` | PASS | 2 | 3310 | 4.0 | 25 |
| `nemotron-3-super:cloud` | PASS | 2 | 2863 | 4.0 | 27 |

**rust-med-02-balanced-brackets**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2703 | 4.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2760 | 4.0 | 19 |
| `glm-5.1:cloud` | PASS | 3 | 3885 | 4.0 | 17 |
| `kimi-k2.6:cloud` | PASS | 2 | 2587 | 4.0 | 25 |
| `minimax-m2.7:cloud` | PASS | 2 | 2552 | 3.0 | 37 |
| `minimax-m3:cloud` | PASS | 2 | 2917 | 3.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 3296 | 3.0 | 21 |

**rust-med-03-roman-to-int**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2900 | 3.0 | 35 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2876 | 3.0 | 29 |
| `glm-5.1:cloud` | PASS | 2 | 2457 | 3.0 | 23 |
| `kimi-k2.6:cloud` | PASS | 2 | 2608 | 4.0 | 27 |
| `minimax-m2.7:cloud` | PASS | 2 | 2466 | 4.0 | 28 |
| `minimax-m3:cloud` | PASS | 2 | 3334 | 4.0 | 31 |
| `nemotron-3-super:cloud` | PASS | 2 | 3191 | 3.0 | 20 |

**rust-med-04-merge-intervals**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3517 | 2.0 | 38 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 4169 | 3.0 | 37 |
| `glm-5.1:cloud` | tests-fail | 2 | 2956 | 1.0 | 21 |
| `kimi-k2.6:cloud` | PASS | 2 | 4501 | 4.0 | 34 |
| `minimax-m2.7:cloud` | PASS | 3 | 7357 | 4.0 | 32 |
| `minimax-m3:cloud` | PASS | 2 | 7219 | 4.0 | 51 |
| `nemotron-3-super:cloud` | PASS | 2 | 5698 | 4.0 | 49 |

**rust-med-05-expr-eval**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 3 | 6564 | 3.0 | 49 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 4499 | 4.0 | 43 |
| `glm-5.1:cloud` | PASS | 2 | 3917 | 4.0 | 30 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3313 | 4.0 | 35 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 4187 | 3.0 | 33 |
| `minimax-m3:cloud` | PASS | 2 | 14422 | 4.0 | 42 |
| `nemotron-3-super:cloud` | PASS | 2 | 5411 | 5.0 | 33 |

**rust-med-06-spiral-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3833 | 1.0 | 72 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3651 | 3.0 | 39 |
| `glm-5.1:cloud` | PASS | 2 | 3381 | 4.0 | 35 |
| `kimi-k2.6:cloud` | PASS | 2 | 5763 | — | 37 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 3273 | 1.0 | 45 |
| `minimax-m3:cloud` | PASS | 2 | 9440 | 4.0 | 48 |
| `nemotron-3-super:cloud` | PASS | 2 | 3883 | 4.0 | 50 |

**rust-med-07-max-subarray**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2794 | 5.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2705 | 5.0 | 16 |
| `glm-5.1:cloud` | tests-fail | 2 | 2276 | 1.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2544 | 4.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 2459 | 5.0 | 16 |
| `minimax-m3:cloud` | PASS | 3 | 5507 | 5.0 | 17 |
| `nemotron-3-super:cloud` | PASS | 2 | 2906 | 4.0 | 16 |

**rust-med-08-top-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2749 | 4.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2765 | — | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2315 | — | 18 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2779 | 3.0 | 24 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2642 | 3.0 | 21 |
| `minimax-m3:cloud` | PASS | 2 | 2924 | 3.0 | 17 |
| `nemotron-3-super:cloud` | PASS | 2 | 2969 | 3.0 | 20 |

**rust-med-09-base-convert**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 3372 | — | 39 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3127 | 4.0 | 26 |
| `glm-5.1:cloud` | PASS | 2 | 2657 | 3.0 | 25 |
| `kimi-k2.6:cloud` | PASS | 2 | 2951 | 3.0 | 26 |
| `minimax-m2.7:cloud` | PASS | 2 | 3366 | 3.0 | 44 |
| `minimax-m3:cloud` | PASS | 2 | 3798 | 3.0 | 32 |
| `nemotron-3-super:cloud` | PASS | 2 | 3690 | 3.0 | 26 |

**rust-med-10-window-max**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3441 | — | 44 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3319 | 3.0 | 38 |
| `glm-5.1:cloud` | PASS | 2 | 2710 | 3.0 | 26 |
| `kimi-k2.6:cloud` | PASS | 3 | 9928 | 4.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 3422 | 5.0 | 40 |
| `minimax-m3:cloud` | PASS | 2 | 4326 | 5.0 | 37 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 3539 | 1.0 | 42 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models glm-5.1:cloud,deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud,minimax-m3:cloud,nemotron-3-super:cloud,kimi-k2.6:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`0e6ab0fd6b95` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
