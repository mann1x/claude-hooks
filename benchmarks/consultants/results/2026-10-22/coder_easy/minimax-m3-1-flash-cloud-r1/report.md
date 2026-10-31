# Skill-Eval Report — coder_easy suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_easy` |
| suite_version | `1.0` |
| suite_hash | `76440a746bddd779...` |
| released | `2026-06-03` |
| run_started_at | `2026-10-22T07:12:33.922720Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.161:11434` |
| judge_model | `(no judge)` |
| git_commit | `8f69cee` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | 180 | 96% (173/180) | 100% (180/180) | 3.8s | 2696 | 4.45 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**minimax-m3.1-flash:cloud** wins the rubric: pass_rate=96%, avg_quality=4.45, median_tokens=2696.

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### easy

**c-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2586 | 5.0 | 8 |

**c-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2920 | 5.0 | 20 |

**c-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2828 | 5.0 | 16 |

**c-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2558 | 5.0 | 10 |

**c-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2611 | 5.0 | 13 |

**c-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2569 | 5.0 | 8 |

**c-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2895 | 4.0 | 22 |

**c-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2990 | 5.0 | 15 |

**c-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2539 | 5.0 | 11 |

**c-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2680 | 5.0 | 12 |

**c-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2616 | 5.0 | 15 |

**c-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2526 | 5.0 | 10 |

**c-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2531 | 5.0 | 6 |

**c-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2614 | 5.0 | 11 |

**c-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2722 | 5.0 | 24 |

**c-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2610 | 5.0 | 12 |

**c-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3042 | 5.0 | 21 |

**c-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3035 | 5.0 | 27 |

**c-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3890 | 4.0 | 83 |

**c-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3251 | 4.0 | 40 |

**c-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3212 | 1.0 | 27 |

**c-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3817 | 1.0 | 28 |

**c-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2592 | 5.0 | 8 |

**c-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2700 | 4.0 | 15 |

**c-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2956 | 1.0 | 20 |

**c-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2534 | 5.0 | 8 |

**c-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2687 | 5.0 | 12 |

**c-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3330 | 2.0 | 39 |

**c-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3358 | 4.0 | 30 |

**c-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2605 | 5.0 | 8 |

**cpp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2617 | 5.0 | 8 |

**cpp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2618 | 5.0 | 9 |

**cpp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2698 | 5.0 | 17 |

**cpp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2594 | 5.0 | 12 |

**cpp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2630 | 5.0 | 16 |

**cpp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2578 | 5.0 | 8 |

**cpp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2690 | 5.0 | 14 |

**cpp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3058 | 5.0 | 18 |

**cpp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2645 | 5.0 | 17 |

**cpp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2660 | 5.0 | 12 |

**cpp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2573 | 5.0 | 11 |

**cpp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2585 | 5.0 | 12 |

**cpp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2531 | 5.0 | 6 |

**cpp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2677 | 5.0 | 15 |

**cpp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2788 | 5.0 | 27 |

**cpp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2592 | 5.0 | 13 |

**cpp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2944 | 4.0 | 23 |

**cpp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2765 | 5.0 | 16 |

**cpp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2877 | 4.0 | 19 |

**cpp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3323 | 5.0 | 22 |

**cpp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2616 | 5.0 | 16 |

**cpp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4267 | 4.0 | 51 |

**cpp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2617 | 5.0 | 12 |

**cpp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2662 | 5.0 | 14 |

**cpp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2947 | 5.0 | 20 |

**cpp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2605 | 5.0 | 12 |

**cpp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2722 | 5.0 | 14 |

**cpp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2996 | 5.0 | 15 |

**cpp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2714 | 5.0 | 11 |

**cpp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2507 | 5.0 | 10 |

**csharp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2714 | 5.0 | 16 |

**csharp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2632 | 5.0 | 16 |

**csharp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2676 | 4.0 | 23 |

**csharp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2883 | 4.0 | 19 |

**csharp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2794 | 3.0 | 30 |

**csharp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2598 | 5.0 | 15 |

**csharp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2640 | 5.0 | 23 |

**csharp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3185 | 5.0 | 30 |

**csharp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2772 | 4.0 | 24 |

**csharp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2694 | 5.0 | 17 |

**csharp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2605 | 5.0 | 23 |

**csharp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2591 | 5.0 | 15 |

**csharp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2508 | 5.0 | 9 |

**csharp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2758 | 4.0 | 29 |

**csharp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2722 | 5.0 | 22 |

**csharp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2683 | 5.0 | 16 |

**csharp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3547 | 5.0 | 33 |

**csharp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3159 | 3.0 | 42 |

**csharp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2868 | 5.0 | 19 |

**csharp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3220 | 3.0 | 39 |

**csharp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2914 | 3.0 | 35 |

**csharp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 3941 | 5.0 | 15 |

**csharp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2783 | 5.0 | 21 |

**csharp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2628 | 5.0 | 16 |

**csharp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2816 | 5.0 | 22 |

**csharp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2655 | 4.0 | 18 |

**csharp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2987 | 4.0 | 30 |

**csharp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2926 | 4.0 | 35 |

**csharp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2672 | 5.0 | 16 |

**csharp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2604 | 5.0 | 17 |

**go-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2968 | 3.0 | 41 |

**go-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2760 | 5.0 | 20 |

**go-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4436 | 5.0 | 22 |

**go-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3075 | 3.0 | 47 |

**go-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3103 | 3.0 | 49 |

**go-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2700 | 5.0 | 19 |

**go-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2712 | 4.0 | 24 |

**go-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3123 | 5.0 | 34 |

**go-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3006 | 3.0 | 51 |

**go-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2695 | 5.0 | 18 |

**go-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2553 | 4.0 | 16 |

**go-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2820 | 3.0 | 36 |

**go-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2589 | 5.0 | 12 |

**go-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3088 | 3.0 | 46 |

**go-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2851 | 5.0 | 40 |

**go-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2931 | 4.0 | 28 |

**go-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3430 | 3.0 | 62 |

**go-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3080 | 3.0 | 38 |

**go-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5827 | 3.0 | 51 |

**go-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2703 | 4.0 | 23 |

**go-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2672 | 4.0 | 22 |

**go-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4364 | 2.0 | 31 |

**go-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2941 | 3.0 | 33 |

**go-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2736 | 5.0 | 23 |

**go-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2787 | 5.0 | 27 |

**go-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3176 | 1.0 | 61 |

**go-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2941 | 4.0 | 33 |

**go-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2903 | 5.0 | 35 |

**go-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3094 | 3.0 | 45 |

**go-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2951 | 3.0 | 41 |

**python-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2536 | 5.0 | 7 |

**python-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2589 | 5.0 | 10 |

**python-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2526 | 5.0 | 3 |

**python-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2452 | 5.0 | 4 |

**python-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2494 | 5.0 | 8 |

**python-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2427 | 5.0 | 4 |

**python-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2483 | 5.0 | 3 |

**python-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3000 | 5.0 | 19 |

**python-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2503 | 5.0 | 10 |

**python-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2562 | 5.0 | 9 |

**python-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2440 | 5.0 | 6 |

**python-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2479 | 4.0 | 7 |

**python-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2452 | 5.0 | 2 |

**python-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2525 | 5.0 | 8 |

**python-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2694 | 5.0 | 22 |

**python-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2631 | 4.0 | 12 |

**python-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2651 | 5.0 | 10 |

**python-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2561 | 5.0 | 4 |

**python-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2734 | 4.0 | 14 |

**python-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2576 | 3.0 | 12 |

**python-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2572 | 5.0 | 9 |

**python-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2622 | 5.0 | 9 |

**python-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2558 | 5.0 | 10 |

**python-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2602 | 3.0 | 12 |

**python-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2677 | 5.0 | 8 |

**python-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2509 | 5.0 | 7 |

**python-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2649 | 5.0 | 11 |

**python-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2670 | 5.0 | 8 |

**python-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2555 | 5.0 | 9 |

**python-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2468 | 4.0 | 9 |

**rust-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2607 | 5.0 | 10 |

**rust-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2761 | 4.0 | 12 |

**rust-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2611 | 5.0 | 10 |

**rust-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2715 | 4.0 | 19 |

**rust-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2691 | 4.0 | 17 |

**rust-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2576 | 5.0 | 11 |

**rust-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2639 | 5.0 | 10 |

**rust-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3086 | 4.0 | 20 |

**rust-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2663 | 5.0 | 17 |

**rust-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2673 | 5.0 | 14 |

**rust-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2493 | 5.0 | 7 |

**rust-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2730 | 5.0 | 14 |

**rust-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2593 | 1.0 | 7 |

**rust-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2716 | 5.0 | 19 |

**rust-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3790 | 4.0 | 69 |

**rust-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2997 | 4.0 | 20 |

**rust-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2875 | 4.0 | 31 |

**rust-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2977 | 5.0 | 25 |

**rust-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2883 | 5.0 | 17 |

**rust-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3185 | 3.0 | 42 |

**rust-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3178 | 4.0 | 36 |

**rust-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 6035 | 4.0 | 87 |

**rust-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2693 | 5.0 | 17 |

**rust-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2713 | 5.0 | 14 |

**rust-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2821 | 5.0 | 18 |

**rust-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2721 | 5.0 | 14 |

**rust-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2842 | 5.0 | 20 |

**rust-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3268 | 4.0 | 32 |

**rust-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2673 | 5.0 | 11 |

**rust-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2569 | 5.0 | 12 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models minimax-m3.1-flash:cloud \
    --ollama-base http://192.168.178.161:11434 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`76440a746bdd` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
