# Skill-Eval Report — coder_easy suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_easy` |
| suite_version | `1.0` |
| suite_hash | `76440a746bddd779...` |
| released | `2026-06-03` |
| run_started_at | `2026-10-22T08:17:32.339497Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.161:11434` |
| judge_model | `(no judge)` |
| git_commit | `8f69cee` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | 180 | 98% (176/180) | 100% (180/180) | 3.5s | 2690 | 4.54 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**minimax-m3.1-flash:cloud** wins the rubric: pass_rate=98%, avg_quality=4.54, median_tokens=2690.

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### easy

**c-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2612 | 5.0 | 8 |

**c-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2988 | 4.0 | 16 |

**c-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3168 | 5.0 | 15 |

**c-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2609 | 5.0 | 10 |

**c-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2580 | 5.0 | 9 |

**c-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2579 | 5.0 | 10 |

**c-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2932 | 5.0 | 24 |

**c-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2995 | 5.0 | 15 |

**c-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2656 | 5.0 | 16 |

**c-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2672 | 5.0 | 12 |

**c-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2602 | 5.0 | 15 |

**c-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2571 | 5.0 | 10 |

**c-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2546 | 5.0 | 6 |

**c-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2647 | 5.0 | 11 |

**c-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2719 | 5.0 | 24 |

**c-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3280 | 5.0 | 9 |

**c-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2866 | 5.0 | 21 |

**c-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3021 | 5.0 | 27 |

**c-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 4077 | 4.0 | 72 |

**c-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3603 | 3.0 | 43 |

**c-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2660 | 5.0 | 18 |

**c-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3895 | 4.0 | 46 |

**c-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2596 | 5.0 | 8 |

**c-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3357 | 4.0 | 39 |

**c-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3123 | 4.0 | 22 |

**c-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2552 | 5.0 | 11 |

**c-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2733 | 5.0 | 12 |

**c-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3031 | 5.0 | 28 |

**c-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3008 | 5.0 | 24 |

**c-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2568 | 5.0 | 8 |

**cpp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2600 | 5.0 | 10 |

**cpp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2576 | 5.0 | 7 |

**cpp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2739 | 4.0 | 17 |

**cpp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2649 | 5.0 | 16 |

**cpp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2592 | 5.0 | 12 |

**cpp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2584 | 5.0 | 10 |

**cpp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2637 | 5.0 | 13 |

**cpp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3069 | 5.0 | 18 |

**cpp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2533 | 5.0 | 11 |

**cpp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2652 | 5.0 | 12 |

**cpp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2551 | 5.0 | 11 |

**cpp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2637 | 5.0 | 12 |

**cpp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2510 | 5.0 | 7 |

**cpp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2641 | 5.0 | 12 |

**cpp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2610 | 5.0 | 10 |

**cpp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2580 | 5.0 | 9 |

**cpp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3684 | 3.0 | 58 |

**cpp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2803 | 5.0 | 14 |

**cpp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2788 | 4.0 | 18 |

**cpp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3003 | 5.0 | 27 |

**cpp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2700 | 5.0 | 18 |

**cpp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4174 | 4.0 | 12 |

**cpp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2639 | 5.0 | 10 |

**cpp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2654 | 5.0 | 12 |

**cpp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2859 | 5.0 | 19 |

**cpp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2582 | 5.0 | 11 |

**cpp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2725 | 5.0 | 14 |

**cpp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2809 | 5.0 | 12 |

**cpp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2699 | 5.0 | 15 |

**cpp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2536 | 5.0 | 10 |

**csharp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2726 | 5.0 | 18 |

**csharp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2628 | 4.0 | 13 |

**csharp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2633 | 5.0 | 24 |

**csharp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2733 | 4.0 | 31 |

**csharp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2535 | 4.0 | 13 |

**csharp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2576 | 5.0 | 15 |

**csharp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2649 | 5.0 | 22 |

**csharp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3046 | 5.0 | 19 |

**csharp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2800 | 4.0 | 24 |

**csharp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2838 | 5.0 | 20 |

**csharp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2561 | 5.0 | 11 |

**csharp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2550 | 5.0 | 15 |

**csharp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2580 | 5.0 | 9 |

**csharp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2749 | 4.0 | 27 |

**csharp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2794 | 5.0 | 26 |

**csharp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2798 | 5.0 | 19 |

**csharp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3404 | 4.0 | 38 |

**csharp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2903 | 5.0 | 26 |

**csharp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2808 | 4.0 | 18 |

**csharp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4405 | 4.0 | 18 |

**csharp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2747 | 4.0 | 25 |

**csharp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2642 | 4.0 | 13 |

**csharp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2685 | 5.0 | 16 |

**csharp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4391 | 5.0 | 16 |

**csharp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2898 | 4.0 | 25 |

**csharp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2603 | 5.0 | 15 |

**csharp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2813 | 5.0 | 22 |

**csharp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2926 | 4.0 | 29 |

**csharp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2656 | 5.0 | 16 |

**csharp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2587 | 5.0 | 17 |

**go-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3063 | 3.0 | 43 |

**go-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2688 | 5.0 | 19 |

**go-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4686 | 4.0 | 22 |

**go-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 5344 | 3.0 | 32 |

**go-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3166 | 3.0 | 46 |

**go-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2582 | 4.0 | 20 |

**go-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2707 | 4.0 | 21 |

**go-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3431 | 3.0 | 38 |

**go-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2710 | 2.0 | 28 |

**go-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2727 | 5.0 | 21 |

**go-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2567 | 4.0 | 16 |

**go-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2836 | 3.0 | 35 |

**go-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2555 | 5.0 | 16 |

**go-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3117 | 3.0 | 48 |

**go-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3730 | 4.0 | 91 |

**go-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2850 | 4.0 | 20 |

**go-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3077 | 3.0 | 56 |

**go-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2909 | 5.0 | 30 |

**go-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3189 | 4.0 | 40 |

**go-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2691 | 5.0 | 24 |

**go-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2632 | 4.0 | 18 |

**go-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3055 | 5.0 | 29 |

**go-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3025 | 3.0 | 43 |

**go-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2689 | 5.0 | 21 |

**go-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2940 | 5.0 | 35 |

**go-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2574 | 5.0 | 18 |

**go-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2831 | 5.0 | 23 |

**go-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2906 | 5.0 | 33 |

**go-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2862 | 4.0 | 32 |

**go-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 4510 | 5.0 | 24 |

**python-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2577 | 5.0 | 7 |

**python-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2690 | 4.0 | 5 |

**python-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2502 | 5.0 | 3 |

**python-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2500 | 5.0 | 8 |

**python-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2434 | 5.0 | 3 |

**python-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2470 | 5.0 | 7 |

**python-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2586 | 5.0 | 10 |

**python-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2986 | 5.0 | 16 |

**python-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2495 | 5.0 | 10 |

**python-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2597 | 5.0 | 11 |

**python-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2400 | 5.0 | 3 |

**python-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2574 | 5.0 | 7 |

**python-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2510 | 5.0 | 2 |

**python-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2522 | 5.0 | 8 |

**python-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2681 | 5.0 | 22 |

**python-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2885 | 5.0 | 12 |

**python-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2565 | 5.0 | 7 |

**python-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2598 | 5.0 | 7 |

**python-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2695 | 4.0 | 14 |

**python-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2535 | 5.0 | 9 |

**python-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2465 | 5.0 | 6 |

**python-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 3626 | 5.0 | 9 |

**python-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2572 | 5.0 | 11 |

**python-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2499 | 4.0 | 8 |

**python-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2676 | 4.0 | 8 |

**python-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2519 | 5.0 | 7 |

**python-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2663 | 5.0 | 13 |

**python-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2660 | 5.0 | 7 |

**python-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2572 | 5.0 | 8 |

**python-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2660 | 4.0 | 13 |

**rust-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2664 | 4.0 | 12 |

**rust-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2760 | 4.0 | 13 |

**rust-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2646 | 5.0 | 12 |

**rust-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2662 | 4.0 | 17 |

**rust-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 4249 | 1.0 | 57 |

**rust-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2568 | 1.0 | 11 |

**rust-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2755 | 2.0 | 20 |

**rust-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3111 | 4.0 | 17 |

**rust-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2607 | 5.0 | 14 |

**rust-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2722 | 5.0 | 15 |

**rust-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2570 | 5.0 | 9 |

**rust-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2620 | 5.0 | 11 |

**rust-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2540 | 1.0 | 7 |

**rust-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2747 | 5.0 | 18 |

**rust-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3650 | 4.0 | 62 |

**rust-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2735 | 4.0 | 17 |

**rust-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2856 | 5.0 | 27 |

**rust-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2769 | 5.0 | 18 |

**rust-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2883 | 5.0 | 17 |

**rust-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3292 | 4.0 | 25 |

**rust-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 3626 | 4.0 | 31 |

**rust-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 3 | 6406 | 5.0 | 75 |

**rust-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2679 | 4.0 | 14 |

**rust-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2823 | 5.0 | 20 |

**rust-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2917 | 5.0 | 22 |

**rust-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2664 | 5.0 | 16 |

**rust-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2782 | 4.0 | 20 |

**rust-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | tests-fail | 2 | 2913 | 5.0 | 18 |

**rust-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2680 | 5.0 | 15 |

**rust-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3.1-flash:cloud` | PASS | 2 | 2539 | 5.0 | 11 |

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
