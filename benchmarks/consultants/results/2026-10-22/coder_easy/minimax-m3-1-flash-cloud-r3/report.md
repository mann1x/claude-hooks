# Skill-Eval Report — coder_easy suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_easy` |
| suite_version | `1.0` |
| suite_hash | `76440a746bddd779...` |
| released | `2026-06-03` |
| run_started_at | `2026-10-22T09:26:26.143640Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.161:11434` |
| judge_model | `(no judge)` |
| git_commit | `8f69cee` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | 180 | 97% (174/180) | 100% (180/180) | 3.3s | 2702 | 4.47 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**minimax-m3.1-flash:cloud** wins the rubric: pass_rate=97%, avg_quality=4.47, median_tokens=2702.

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### easy

**c-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2574 | 5.0 | 8 |

**c-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2672 | 4.0 | 15 |

**c-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2679 | 5.0 | 12 |

**c-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2571 | 5.0 | 9 |

**c-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2604 | 5.0 | 10 |

**c-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2578 | 5.0 | 8 |

**c-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2714 | 4.0 | 16 |

**c-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2988 | 5.0 | 15 |

**c-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2638 | 5.0 | 17 |

**c-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2801 | 5.0 | 13 |

**c-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2619 | 5.0 | 14 |

**c-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2635 | 5.0 | 11 |

**c-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2524 | 5.0 | 6 |

**c-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2642 | 5.0 | 13 |

**c-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2678 | 5.0 | 15 |

**c-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2671 | 5.0 | 11 |

**c-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 4 | 7157 | 5.0 | 21 |

**c-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3036 | 5.0 | 27 |

**c-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4253 | 5.0 | 83 |

**c-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3285 | 2.0 | 27 |

**c-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4272 | 4.0 | 44 |

**c-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2697 | 4.0 | 10 |

**c-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2623 | 5.0 | 9 |

**c-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2679 | 4.0 | 17 |

**c-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2893 | 5.0 | 19 |

**c-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2553 | 5.0 | 11 |

**c-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2735 | 5.0 | 12 |

**c-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3638 | 5.0 | 41 |

**c-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2958 | 5.0 | 24 |

**c-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2488 | 5.0 | 8 |

**cpp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2585 | 5.0 | 10 |

**cpp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2672 | 5.0 | 8 |

**cpp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2744 | 5.0 | 18 |

**cpp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2599 | 5.0 | 14 |

**cpp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2651 | 5.0 | 17 |

**cpp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2550 | 5.0 | 8 |

**cpp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2724 | 5.0 | 10 |

**cpp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3032 | 5.0 | 14 |

**cpp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2534 | 5.0 | 11 |

**cpp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2674 | 5.0 | 12 |

**cpp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2636 | 5.0 | 17 |

**cpp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2600 | 5.0 | 13 |

**cpp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2522 | 5.0 | 6 |

**cpp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2645 | 5.0 | 12 |

**cpp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2595 | 4.0 | 10 |

**cpp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2581 | 5.0 | 9 |

**cpp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3132 | 4.0 | 27 |

**cpp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2748 | 5.0 | 14 |

**cpp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2814 | 5.0 | 19 |

**cpp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 3 | 5316 | 2.0 | 31 |

**cpp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2674 | 5.0 | 18 |

**cpp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3689 | 5.0 | 44 |

**cpp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2610 | 5.0 | 10 |

**cpp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2659 | 5.0 | 14 |

**cpp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2882 | 5.0 | 20 |

**cpp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2596 | 5.0 | 10 |

**cpp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2718 | 5.0 | 14 |

**cpp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2816 | 5.0 | 17 |

**cpp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2689 | 5.0 | 11 |

**cpp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2536 | 5.0 | 10 |

**csharp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2673 | 5.0 | 17 |

**csharp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2809 | 5.0 | 12 |

**csharp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2684 | 5.0 | 27 |

**csharp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2749 | 4.0 | 22 |

**csharp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2745 | 4.0 | 24 |

**csharp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2582 | 5.0 | 16 |

**csharp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2701 | 4.0 | 19 |

**csharp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3054 | 5.0 | 21 |

**csharp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2612 | 5.0 | 18 |

**csharp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2717 | 4.0 | 21 |

**csharp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2585 | 5.0 | 23 |

**csharp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2581 | 5.0 | 15 |

**csharp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2506 | 5.0 | 9 |

**csharp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2728 | 4.0 | 28 |

**csharp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2732 | 5.0 | 22 |

**csharp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2619 | 5.0 | 19 |

**csharp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5086 | 4.0 | 35 |

**csharp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2757 | 4.0 | 20 |

**csharp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5302 | 3.0 | 28 |

**csharp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2622 | 5.0 | 18 |

**csharp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2876 | 1.0 | 25 |

**csharp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 3971 | 5.0 | 15 |

**csharp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2665 | 5.0 | 18 |

**csharp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2659 | 5.0 | 15 |

**csharp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2926 | 4.0 | 29 |

**csharp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2867 | 5.0 | 21 |

**csharp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2862 | 5.0 | 22 |

**csharp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2914 | 4.0 | 22 |

**csharp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2671 | 5.0 | 17 |

**csharp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2786 | 5.0 | 18 |

**go-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3027 | 3.0 | 41 |

**go-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2748 | 5.0 | 19 |

**go-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2905 | 3.0 | 39 |

**go-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2989 | 3.0 | 45 |

**go-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4922 | 3.0 | 42 |

**go-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2579 | 5.0 | 20 |

**go-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2695 | 5.0 | 24 |

**go-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3109 | 5.0 | 29 |

**go-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2762 | 4.0 | 31 |

**go-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2714 | 5.0 | 23 |

**go-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2775 | 3.0 | 26 |

**go-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2830 | 3.0 | 39 |

**go-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2570 | 5.0 | 17 |

**go-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3086 | 3.0 | 47 |

**go-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2745 | 4.0 | 34 |

**go-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2704 | 5.0 | 22 |

**go-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3210 | 3.0 | 56 |

**go-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5981 | 3.0 | 71 |

**go-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3357 | 3.0 | 55 |

**go-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2656 | 5.0 | 19 |

**go-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2585 | 5.0 | 20 |

**go-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4260 | 2.0 | 26 |

**go-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4932 | 4.0 | 32 |

**go-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2694 | 5.0 | 20 |

**go-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2887 | 5.0 | 29 |

**go-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2679 | 5.0 | 20 |

**go-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3240 | 2.0 | 44 |

**go-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2964 | 5.0 | 33 |

**go-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4710 | 2.0 | 30 |

**go-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3009 | 3.0 | 41 |

**python-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2532 | 5.0 | 7 |

**python-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2553 | 5.0 | 5 |

**python-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2469 | 5.0 | 3 |

**python-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2484 | 5.0 | 8 |

**python-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2468 | 5.0 | 6 |

**python-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2477 | 5.0 | 6 |

**python-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2557 | 5.0 | 10 |

**python-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3028 | 5.0 | 19 |

**python-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2542 | 5.0 | 13 |

**python-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2552 | 5.0 | 9 |

**python-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2443 | 5.0 | 7 |

**python-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2592 | 5.0 | 10 |

**python-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2445 | 5.0 | 2 |

**python-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2472 | 5.0 | 3 |

**python-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3449 | 4.0 | 46 |

**python-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2723 | 5.0 | 11 |

**python-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2552 | 5.0 | 6 |

**python-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2600 | 5.0 | 7 |

**python-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2762 | 5.0 | 18 |

**python-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2458 | 5.0 | 6 |

**python-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2502 | 5.0 | 9 |

**python-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 3978 | 5.0 | 6 |

**python-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2590 | 4.0 | 14 |

**python-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2618 | 5.0 | 12 |

**python-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2757 | 4.0 | 8 |

**python-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2454 | 5.0 | 3 |

**python-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2652 | 5.0 | 11 |

**python-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2768 | 4.0 | 12 |

**python-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2555 | 5.0 | 9 |

**python-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2464 | 4.0 | 9 |

**rust-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2680 | 5.0 | 10 |

**rust-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2878 | 4.0 | 16 |

**rust-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2637 | 5.0 | 10 |

**rust-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2668 | 4.0 | 16 |

**rust-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2644 | 4.0 | 17 |

**rust-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2583 | 5.0 | 11 |

**rust-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2721 | 4.0 | 15 |

**rust-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3106 | 4.0 | 17 |

**rust-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2706 | 5.0 | 17 |

**rust-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2715 | 5.0 | 14 |

**rust-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2477 | 5.0 | 6 |

**rust-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2604 | 5.0 | 11 |

**rust-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2618 | 1.0 | 7 |

**rust-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2710 | 5.0 | 17 |

**rust-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2805 | 5.0 | 26 |

**rust-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3091 | 4.0 | 20 |

**rust-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2859 | 5.0 | 26 |

**rust-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2704 | 5.0 | 9 |

**rust-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3271 | 3.0 | 20 |

**rust-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 3374 | 1.0 | 46 |

**rust-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3824 | 3.0 | 35 |

**rust-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 5 | 15742 | 4.0 | 67 |

**rust-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2725 | 4.0 | 14 |

**rust-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2654 | 5.0 | 13 |

**rust-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2974 | 1.0 | 24 |

**rust-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3034 | 4.0 | 19 |

**rust-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2891 | 5.0 | 24 |

**rust-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2960 | 2.0 | 23 |

**rust-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2661 | 5.0 | 12 |

**rust-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2746 | 4.0 | 14 |

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
