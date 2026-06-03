# Skill-Eval Report — coder_easy suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_easy` |
| suite_version | `1.0` |
| suite_hash | `76440a746bddd779...` |
| released | `2026-06-03` |
| run_started_at | `2026-06-03T19:21:35.170209Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `d0fc724` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 180 | 99% (178/180) | 100% (180/180) | 17.8s | 2416 | 3.90 | ✅ |
| `glm-5.1:cloud` | 180 | 99% (178/180) | 100% (180/180) | 16.1s | 1944 | 4.16 | ✅ |
| `deepseek-v4-pro:cloud` | 180 | 98% (177/180) | 100% (180/180) | 17.4s | 2418 | 4.13 | ✅ |
| `kimi-k2.6:cloud` | 180 | 98% (177/180) | 100% (180/180) | 19.9s | 2173 | 4.31 | ✅ |
| `minimax-m3:cloud` | 180 | 98% (176/180) | 100% (180/180) | 18.6s | 249 | 4.14 | ✅ |
| `minimax-m2.7:cloud` | 180 | 97% (174/180) | 100% (180/180) | 19.4s | 1911 | 3.74 | ✅ |
| `nemotron-3-super:cloud` | 180 | 93% (168/180) | 99% (179/180) | 17.7s | 2706 | 3.92 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**glm-5.1:cloud** wins the rubric: pass_rate=99%, avg_quality=4.16, median_tokens=1944. Also qualifying: deepseek-v4-flash:cloud (99%/3.90), kimi-k2.6:cloud (98%/4.31), deepseek-v4-pro:cloud (98%/4.13), minimax-m3:cloud (98%/4.14), minimax-m2.7:cloud (97%/3.74), nemotron-3-super:cloud (93%/3.92).

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### easy

**c-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2374 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2372 | 5.0 | 6 |
| `glm-5.1:cloud` | PASS | 2 | 1942 | 5.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2141 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1807 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 143 | 5.0 | 6 |
| `nemotron-3-super:cloud` | PASS | 2 | 2713 | 5.0 | 9 |

**c-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2531 | 4.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2520 | 4.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 2006 | 4.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 3309 | 3.0 | 16 |
| `minimax-m2.7:cloud` | PASS | 2 | 1760 | 3.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 266 | 3.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 3401 | 4.0 | 18 |

**c-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2431 | 5.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2437 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1981 | 5.0 | 11 |
| `kimi-k2.6:cloud` | PASS | 2 | 2306 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1865 | 4.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 204 | 5.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 2919 | 5.0 | 12 |

**c-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2353 | 5.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2330 | 5.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 1904 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 1927 | 5.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 1746 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 501 | 4.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2664 | 3.0 | 15 |

**c-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2349 | 5.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2322 | 5.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 1894 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 2202 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1744 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 186 | 5.0 | 9 |
| `nemotron-3-super:cloud` | PASS | 2 | 2684 | 5.0 | 10 |

**c-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2367 | 5.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2352 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1900 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 1950 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1865 | 5.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 191 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2627 | 5.0 | 12 |

**c-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2508 | 4.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2558 | 5.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2076 | 4.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 2332 | 4.0 | 23 |
| `minimax-m2.7:cloud` | PASS | 2 | 1962 | 2.0 | 18 |
| `minimax-m3:cloud` | PASS | 2 | 249 | 4.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 3120 | 2.0 | 18 |

**c-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2771 | 4.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2774 | 5.0 | 15 |
| `glm-5.1:cloud` | PASS | 2 | 2524 | 3.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 2772 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2237 | 5.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 249 | 4.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 3291 | 5.0 | 11 |

**c-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2346 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2373 | 5.0 | 14 |
| `glm-5.1:cloud` | PASS | 2 | 1914 | 5.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 1869 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1790 | 5.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 182 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2622 | 5.0 | 16 |

**c-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2590 | 3.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2942 | 4.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 2023 | 5.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 2601 | 5.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 2181 | 4.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 269 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2778 | 4.0 | 18 |

**c-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2367 | 5.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2512 | 5.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 1948 | 5.0 | 13 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2076 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2381 | 2.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 279 | 5.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2765 | 5.0 | 17 |

**c-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2421 | 5.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2340 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1928 | 4.0 | 8 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2118 | 5.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 2002 | 2.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 248 | 5.0 | 9 |
| `nemotron-3-super:cloud` | PASS | 2 | 2736 | 5.0 | 11 |

**c-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2311 | 5.0 | 6 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2341 | 5.0 | 6 |
| `glm-5.1:cloud` | PASS | 2 | 1912 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 2026 | 4.0 | 6 |
| `minimax-m2.7:cloud` | PASS | 2 | 1734 | 5.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 366 | 5.0 | 6 |
| `nemotron-3-super:cloud` | PASS | 2 | 2548 | 5.0 | 9 |

**c-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2404 | 2.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2390 | 2.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1954 | 2.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2111 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1937 | 5.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 201 | 5.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 2619 | 5.0 | 13 |

**c-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2376 | 4.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2467 | 4.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 1950 | 5.0 | 9 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2329 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1865 | 4.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 257 | 1.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2797 | 4.0 | 23 |

**c-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2416 | 4.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2390 | 4.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1939 | 5.0 | 10 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3184 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 2064 | 5.0 | 7 |
| `minimax-m3:cloud` | ERROR | 2 | 205 | 3.0 | 9 |
| `nemotron-3-super:cloud` | PASS | 2 | 3262 | 4.0 | 9 |

**c-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3866 | 5.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2805 | 2.0 | 20 |
| `glm-5.1:cloud` | PASS | 2 | 3008 | — | 13 |
| `kimi-k2.6:cloud` | ERROR | 2 | 5859 | 5.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 3841 | — | 33 |
| `minimax-m3:cloud` | PASS | 2 | 1162 | — | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 3956 | — | 14 |

**c-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2864 | 2.0 | 27 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2863 | 3.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2289 | 4.0 | 23 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2787 | 2.0 | 24 |
| `minimax-m2.7:cloud` | PASS | 2 | 1999 | 2.0 | 15 |
| `minimax-m3:cloud` | PASS | 2 | 490 | 5.0 | 25 |
| `nemotron-3-super:cloud` | PASS | 2 | 3190 | 4.0 | 34 |

**c-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 3321 | 4.0 | 24 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2807 | 2.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2329 | 2.0 | 20 |
| `kimi-k2.6:cloud` | PASS | 2 | 6974 | 3.0 | 58 |
| `minimax-m2.7:cloud` | PASS | 2 | 2389 | 2.0 | 24 |
| `minimax-m3:cloud` | PASS | 2 | 866 | 3.0 | 24 |
| `nemotron-3-super:cloud` | PASS | 2 | 5065 | 2.0 | 82 |

**c-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2318 | 5.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2436 | 2.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1891 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 2043 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1805 | 2.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 585 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 3086 | 2.0 | 8 |

**c-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2485 | 3.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2427 | 4.0 | 18 |
| `glm-5.1:cloud` | PASS | 2 | 2003 | 5.0 | 19 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3054 | 5.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 2341 | — | 19 |
| `minimax-m3:cloud` | PASS | 2 | 253 | 4.0 | 18 |
| `nemotron-3-super:cloud` | PASS | 2 | 2813 | 5.0 | 21 |

**c-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2469 | 2.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2468 | 3.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1995 | 4.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 2603 | — | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1974 | 4.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 317 | 3.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2878 | 3.0 | 10 |

**c-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2377 | 5.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2383 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1943 | 5.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2218 | 5.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 1842 | 5.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 488 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2545 | 5.0 | 10 |

**c-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2416 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2806 | 4.0 | 23 |
| `glm-5.1:cloud` | PASS | 2 | 2190 | 4.0 | 20 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2890 | 4.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 1965 | 4.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 867 | 3.0 | 34 |
| `nemotron-3-super:cloud` | PASS | 2 | 2993 | 3.0 | 21 |

**c-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2569 | 5.0 | 22 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3552 | 5.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2221 | 4.0 | 22 |
| `kimi-k2.6:cloud` | PASS | 2 | 2339 | 5.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 1949 | 3.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 395 | — | 34 |
| `nemotron-3-super:cloud` | PASS | 2 | 3096 | 5.0 | 29 |

**c-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2342 | 5.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2432 | 5.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 1915 | 5.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 1830 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1793 | 5.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 264 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2536 | 5.0 | 10 |

**c-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2506 | 5.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2612 | 5.0 | 15 |
| `glm-5.1:cloud` | PASS | 2 | 2063 | 5.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 2543 | 5.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 1885 | 5.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 270 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2665 | 5.0 | 13 |

**c-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2740 | 5.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3056 | 5.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2272 | 5.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 3480 | 4.0 | 26 |
| `minimax-m2.7:cloud` | PASS | 2 | 2307 | 2.0 | 27 |
| `minimax-m3:cloud` | PASS | 2 | 844 | 4.0 | 24 |
| `nemotron-3-super:cloud` | PASS | 2 | 4060 | 4.0 | 46 |

**c-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2647 | 2.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2694 | 5.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2041 | 2.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2549 | 5.0 | 30 |
| `minimax-m2.7:cloud` | PASS | 2 | 2245 | 2.0 | 16 |
| `minimax-m3:cloud` | PASS | 2 | 611 | 5.0 | 21 |
| `nemotron-3-super:cloud` | PASS | 2 | 2908 | 5.0 | 34 |

**c-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2301 | 5.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2302 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1880 | 5.0 | 9 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2334 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1736 | 5.0 | 9 |
| `minimax-m3:cloud` | ERROR | 2 | 165 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2441 | 5.0 | 9 |

**cpp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2368 | 4.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2401 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1933 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 1957 | 5.0 | 6 |
| `minimax-m2.7:cloud` | PASS | 2 | 1743 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 128 | 5.0 | 6 |
| `nemotron-3-super:cloud` | PASS | 2 | 2527 | 4.0 | 10 |

**cpp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2309 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2308 | 5.0 | 6 |
| `glm-5.1:cloud` | PASS | 2 | 1894 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 1876 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2121 | 5.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 147 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2526 | 5.0 | 8 |

**cpp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2491 | 3.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2498 | 4.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 2106 | 4.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2154 | 5.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 2256 | 5.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 234 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2619 | 5.0 | 13 |

**cpp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2321 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2342 | 4.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1899 | 4.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 1961 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1750 | 3.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 208 | 5.0 | 9 |
| `nemotron-3-super:cloud` | PASS | 2 | 2564 | 5.0 | 10 |

**cpp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2353 | 3.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2351 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1883 | 5.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2338 | 4.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 1952 | 4.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 170 | 4.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2615 | 4.0 | 9 |

**cpp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2340 | 5.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2417 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1878 | 3.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 1967 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1730 | 4.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 177 | 4.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2515 | 3.0 | 11 |

**cpp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2335 | 5.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2338 | 5.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 1896 | 4.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 2385 | 5.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 1870 | 4.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 323 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2947 | 4.0 | 13 |

**cpp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2765 | 5.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2773 | 5.0 | 15 |
| `glm-5.1:cloud` | PASS | 2 | 2289 | 4.0 | 13 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2336 | 4.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 2282 | 4.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 223 | 4.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 3082 | 5.0 | 17 |

**cpp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2340 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2332 | 4.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1841 | 3.0 | 7 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1758 | 5.0 | 6 |
| `minimax-m2.7:cloud` | PASS | 2 | 1751 | 4.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 156 | 4.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2487 | 4.0 | 15 |

**cpp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2620 | 5.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2476 | 5.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 1975 | 3.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2435 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2018 | 3.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 269 | 3.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2985 | 3.0 | 17 |

**cpp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2430 | 4.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2326 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1913 | 5.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2001 | 5.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 1817 | 5.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 579 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2504 | 4.0 | 13 |

**cpp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2347 | 5.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2319 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1902 | 5.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 1966 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1757 | 4.0 | 10 |
| `minimax-m3:cloud` | tests-fail | 2 | 208 | 1.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2632 | 4.0 | 15 |

**cpp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2308 | 5.0 | 6 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2357 | 5.0 | 5 |
| `glm-5.1:cloud` | PASS | 2 | 1880 | 4.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 1950 | 5.0 | 6 |
| `minimax-m2.7:cloud` | PASS | 2 | 2016 | 4.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 304 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2579 | 4.0 | 8 |

**cpp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2426 | 2.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2376 | 2.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1932 | 4.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 1851 | 3.0 | 1 |
| `minimax-m2.7:cloud` | PASS | 2 | 1841 | 2.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 180 | 4.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2544 | 3.0 | 12 |

**cpp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2364 | 2.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2465 | 4.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 1946 | 4.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2125 | 4.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 1789 | 4.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 340 | 4.0 | 24 |
| `nemotron-3-super:cloud` | PASS | 2 | 2707 | 4.0 | 16 |

**cpp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2371 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2361 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1917 | 5.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2033 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1718 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 210 | 5.0 | 9 |
| `nemotron-3-super:cloud` | PASS | 2 | 2512 | 3.0 | 10 |

**cpp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2635 | 3.0 | 25 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2697 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1966 | 4.0 | 9 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2136 | 4.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 2321 | 4.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 1500 | — | 18 |
| `nemotron-3-super:cloud` | ERROR | 1 | 66566 | — | 0 |

**cpp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2539 | 5.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2544 | 5.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 2063 | 4.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2187 | 4.0 | 16 |
| `minimax-m2.7:cloud` | PASS | 2 | 1922 | 4.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 265 | 4.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 2820 | 5.0 | 14 |

**cpp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2663 | 5.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2600 | 5.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 2153 | 5.0 | 15 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2477 | 5.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 2242 | 3.0 | 18 |
| `minimax-m3:cloud` | PASS | 2 | 319 | 5.0 | 20 |
| `nemotron-3-super:cloud` | PASS | 2 | 2664 | 3.0 | 22 |

**cpp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2332 | 2.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2340 | 4.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1896 | 4.0 | 9 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2159 | 4.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 1745 | 4.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 214 | 4.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2606 | 5.0 | 12 |

**cpp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2472 | 3.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2483 | 4.0 | 16 |
| `glm-5.1:cloud` | PASS | 2 | 1941 | 4.0 | 16 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2255 | 3.0 | 18 |
| `minimax-m2.7:cloud` | PASS | 2 | 1943 | 3.0 | 16 |
| `minimax-m3:cloud` | PASS | 2 | 394 | 5.0 | 18 |
| `nemotron-3-super:cloud` | PASS | 2 | 2765 | 5.0 | 17 |

**cpp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2525 | 3.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2482 | 4.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 2047 | 4.0 | 17 |
| `kimi-k2.6:cloud` | PASS | 2 | 2658 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2150 | 4.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 348 | 2.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2964 | 4.0 | 15 |

**cpp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2354 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2380 | 4.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1938 | 5.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2121 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1796 | 5.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 226 | 5.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 2512 | 4.0 | 12 |

**cpp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2373 | 5.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2347 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1949 | 5.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2151 | 5.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 1783 | 5.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 255 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2534 | 5.0 | 17 |

**cpp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2631 | 3.0 | 19 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2683 | 1.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2126 | 4.0 | 17 |
| `kimi-k2.6:cloud` | PASS | 2 | 3036 | 5.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 2084 | 3.0 | 19 |
| `minimax-m3:cloud` | PASS | 2 | 770 | 3.0 | 20 |
| `nemotron-3-super:cloud` | PASS | 2 | 2679 | 4.0 | 21 |

**cpp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2330 | 4.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2347 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1891 | 4.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 1851 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1762 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 170 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2797 | 4.0 | 10 |

**cpp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2515 | 5.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2573 | 5.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 2033 | 5.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 2488 | 4.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 1989 | 4.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 283 | 4.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 2672 | 3.0 | 19 |

**cpp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2690 | 5.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2520 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 2023 | 4.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2278 | 4.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1931 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 288 | 4.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 2732 | 4.0 | 18 |

**cpp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2455 | 4.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2427 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1966 | 3.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2154 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1815 | 4.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 212 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2840 | 4.0 | 11 |

**cpp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2289 | 5.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2291 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1846 | 5.0 | 5 |
| `kimi-k2.6:cloud` | PASS | 2 | 1865 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1848 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 193 | 4.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2326 | 4.0 | 9 |

**csharp-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2427 | 2.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2394 | 4.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 1983 | 5.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2516 | 5.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 1869 | 4.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 177 | 3.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2796 | 3.0 | 23 |

**csharp-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2308 | 5.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2289 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1871 | 5.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 1856 | 5.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 1672 | 5.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 481 | 5.0 | 4 |
| `nemotron-3-super:cloud` | PASS | 2 | 2668 | 5.0 | 13 |

**csharp-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2429 | 5.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2346 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1981 | 4.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2146 | 4.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 1784 | 5.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 220 | 3.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 2548 | 4.0 | 16 |

**csharp-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2337 | 5.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2356 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1883 | 2.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 1855 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1715 | 2.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 194 | 3.0 | 17 |
| `nemotron-3-super:cloud` | PASS | 2 | 2548 | 5.0 | 10 |

**csharp-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2329 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2324 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1888 | 5.0 | 12 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2367 | 4.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1723 | 4.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 208 | 3.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2932 | 3.0 | 16 |

**csharp-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2353 | 5.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2374 | 5.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 1880 | 5.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 1853 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2162 | 5.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 171 | 5.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2698 | 4.0 | 14 |

**csharp-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2350 | 5.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2400 | 5.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 1986 | 4.0 | 12 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2101 | 5.0 | 19 |
| `minimax-m2.7:cloud` | PASS | 2 | 1708 | 5.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 182 | 5.0 | 9 |
| `nemotron-3-super:cloud` | PASS | 2 | 2540 | 5.0 | 18 |

**csharp-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2752 | 4.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2758 | 5.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2273 | 4.0 | 15 |
| `kimi-k2.6:cloud` | PASS | 2 | 2506 | 4.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2257 | 5.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 244 | 4.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 3100 | 4.0 | 19 |

**csharp-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2398 | 4.0 | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2519 | 5.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 1978 | 4.0 | 17 |
| `kimi-k2.6:cloud` | PASS | 2 | 2716 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1788 | 4.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 222 | 5.0 | 23 |
| `nemotron-3-super:cloud` | PASS | 2 | 2539 | 5.0 | 17 |

**csharp-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2637 | 4.0 | 18 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2544 | 4.0 | 26 |
| `glm-5.1:cloud` | PASS | 3 | 3601 | 5.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2451 | 3.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 1886 | 4.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 250 | 4.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 3220 | 4.0 | 15 |

**csharp-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2359 | 3.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 3021 | 4.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1928 | 4.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 2620 | 4.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2043 | 5.0 | 15 |
| `minimax-m3:cloud` | PASS | 2 | 199 | 3.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2617 | 4.0 | 15 |

**csharp-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2335 | 4.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2300 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1905 | 4.0 | 11 |
| `kimi-k2.6:cloud` | PASS | 2 | 1952 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1739 | 4.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 508 | 4.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 2504 | 5.0 | 12 |

**csharp-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2326 | 5.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2319 | 4.0 | 2 |
| `glm-5.1:cloud` | PASS | 2 | 1934 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 1966 | 5.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1990 | 5.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 132 | 5.0 | 9 |
| `nemotron-3-super:cloud` | PASS | 2 | 2459 | 5.0 | 11 |

**csharp-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2378 | 2.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2403 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1940 | 4.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2375 | 3.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1829 | 2.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 325 | 3.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2707 | 3.0 | 12 |

**csharp-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2490 | 4.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2519 | 4.0 | 16 |
| `glm-5.1:cloud` | PASS | 2 | 1936 | 4.0 | 11 |
| `kimi-k2.6:cloud` | PASS | 2 | 1966 | 4.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 1851 | 4.0 | 15 |
| `minimax-m3:cloud` | PASS | 2 | 228 | 4.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 2946 | 4.0 | 29 |

**csharp-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2270 | 2.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2271 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1818 | 4.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 1931 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1971 | 4.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 184 | 4.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2876 | 3.0 | 15 |

**csharp-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2428 | 4.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2438 | 2.0 | 14 |
| `glm-5.1:cloud` | PASS | 2 | 2008 | 4.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 2414 | 5.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 2252 | 4.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 576 | 4.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 2741 | 4.0 | 14 |

**csharp-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2482 | 2.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2530 | 5.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 2007 | 5.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 2162 | 5.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1858 | 2.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 186 | 4.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2803 | 5.0 | 16 |

**csharp-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2612 | 3.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2676 | 2.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2146 | 4.0 | 21 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2183 | 5.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 1973 | 2.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 298 | 3.0 | 24 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2947 | 3.0 | 20 |

**csharp-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2249 | 4.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2244 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1826 | 3.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 1991 | 4.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1789 | 4.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 173 | 4.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2511 | 4.0 | 10 |

**csharp-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2266 | 5.0 | 9 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2279 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1838 | 4.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2545 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1667 | 5.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 178 | 4.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2992 | 3.0 | 21 |

**csharp-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2522 | 3.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2760 | 4.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 1950 | 3.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2460 | 4.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 1973 | 3.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 202 | 3.0 | 13 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 3565 | 1.0 | 22 |

**csharp-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2411 | 2.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2428 | 5.0 | 15 |
| `glm-5.1:cloud` | PASS | 2 | 1988 | 5.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 2096 | 4.0 | 18 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2145 | 1.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 254 | 5.0 | 12 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2842 | 4.0 | 20 |

**csharp-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2426 | 2.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2433 | 2.0 | 16 |
| `glm-5.1:cloud` | PASS | 2 | 2006 | 4.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 2030 | 4.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 2129 | 4.0 | 15 |
| `minimax-m3:cloud` | PASS | 2 | 854 | 4.0 | 17 |
| `nemotron-3-super:cloud` | PASS | 2 | 2551 | 4.0 | 18 |

**csharp-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2463 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2527 | 4.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 2006 | 4.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2640 | 4.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 2123 | 4.0 | 18 |
| `minimax-m3:cloud` | PASS | 2 | 779 | 4.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 2764 | 4.0 | 19 |

**csharp-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2380 | 5.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2381 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1930 | 5.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2852 | 5.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 2076 | 2.0 | 16 |
| `minimax-m3:cloud` | PASS | 2 | 204 | 4.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 4504 | 4.0 | 23 |

**csharp-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2526 | 2.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2571 | 4.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2146 | 4.0 | 20 |
| `kimi-k2.6:cloud` | PASS | 2 | 2769 | 4.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 1910 | 2.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 796 | 4.0 | 20 |
| `nemotron-3-super:cloud` | PASS | 2 | 2872 | 4.0 | 23 |

**csharp-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2421 | 5.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2592 | 4.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2089 | 5.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 2269 | 5.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 1958 | 5.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 335 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2999 | 4.0 | 21 |

**csharp-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2473 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2442 | 2.0 | 14 |
| `glm-5.1:cloud` | PASS | 2 | 2005 | 4.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 2786 | 5.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 1763 | 4.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 200 | 4.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2700 | 2.0 | 14 |

**csharp-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2321 | 3.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2338 | 4.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 1868 | 5.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 2366 | 4.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 2040 | 2.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 240 | 4.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2975 | 2.0 | 24 |

**go-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2465 | 3.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2502 | 3.0 | 24 |
| `glm-5.1:cloud` | PASS | 2 | 2021 | 3.0 | 19 |
| `kimi-k2.6:cloud` | PASS | 2 | 2216 | 3.0 | 16 |
| `minimax-m2.7:cloud` | PASS | 2 | 1860 | 2.0 | 19 |
| `minimax-m3:cloud` | PASS | 2 | 336 | 3.0 | 28 |
| `nemotron-3-super:cloud` | PASS | 2 | 2650 | 3.0 | 25 |

**go-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2463 | 4.0 | 19 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2678 | 3.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2059 | 4.0 | 18 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2083 | 5.0 | 16 |
| `minimax-m2.7:cloud` | PASS | 2 | 1762 | 4.0 | 16 |
| `minimax-m3:cloud` | tests-fail | 2 | 764 | 3.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 3577 | 3.0 | 23 |

**go-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2427 | 3.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2533 | 4.0 | 22 |
| `glm-5.1:cloud` | tests-fail | 2 | 1983 | 1.0 | 19 |
| `kimi-k2.6:cloud` | PASS | 2 | 3255 | 4.0 | 18 |
| `minimax-m2.7:cloud` | PASS | 2 | 1884 | 4.0 | 19 |
| `minimax-m3:cloud` | PASS | 2 | 281 | 3.0 | 25 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2579 | 2.0 | 17 |

**go-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2457 | 2.0 | 22 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2423 | 2.0 | 23 |
| `glm-5.1:cloud` | PASS | 2 | 1937 | 4.0 | 20 |
| `kimi-k2.6:cloud` | PASS | 2 | 2076 | 3.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 1829 | 3.0 | 21 |
| `minimax-m3:cloud` | PASS | 2 | 212 | 4.0 | 22 |
| `nemotron-3-super:cloud` | PASS | 2 | 2907 | 3.0 | 35 |

**go-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2439 | 2.0 | 22 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2361 | 3.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 1963 | 3.0 | 21 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2208 | 3.0 | 19 |
| `minimax-m2.7:cloud` | PASS | 2 | 1963 | 3.0 | 21 |
| `minimax-m3:cloud` | PASS | 2 | 176 | 4.0 | 23 |
| `nemotron-3-super:cloud` | PASS | 2 | 3008 | 3.0 | 36 |

**go-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2282 | 5.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2367 | 4.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1874 | 4.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 1900 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1904 | 3.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 249 | 3.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2511 | 3.0 | 19 |

**go-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2504 | 3.0 | 26 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2533 | 4.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 1997 | 3.0 | 21 |
| `kimi-k2.6:cloud` | PASS | 2 | 2163 | 3.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 2175 | 4.0 | 25 |
| `minimax-m3:cloud` | PASS | 2 | 289 | 3.0 | 29 |
| `nemotron-3-super:cloud` | PASS | 2 | 2999 | 2.0 | 30 |

**go-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2712 | 4.0 | 18 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2868 | 3.0 | 25 |
| `glm-5.1:cloud` | PASS | 2 | 2353 | 3.0 | 24 |
| `kimi-k2.6:cloud` | PASS | 2 | 2416 | 5.0 | 18 |
| `minimax-m2.7:cloud` | PASS | 2 | 2270 | 4.0 | 23 |
| `minimax-m3:cloud` | PASS | 2 | 247 | 4.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 3160 | 3.0 | 31 |

**go-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2305 | 5.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2320 | 5.0 | 15 |
| `glm-5.1:cloud` | PASS | 2 | 1881 | 4.0 | 15 |
| `kimi-k2.6:cloud` | PASS | 2 | 1976 | 4.0 | 13 |
| `minimax-m2.7:cloud` | PASS | 2 | 1758 | 3.0 | 19 |
| `minimax-m3:cloud` | PASS | 2 | 185 | 5.0 | 18 |
| `nemotron-3-super:cloud` | PASS | 2 | 2528 | 4.0 | 18 |

**go-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2508 | 5.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2515 | 3.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2015 | 4.0 | 13 |
| `kimi-k2.6:cloud` | PASS | 2 | 2016 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1934 | 3.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 317 | 4.0 | 18 |
| `nemotron-3-super:cloud` | PASS | 2 | 2887 | 3.0 | 27 |

**go-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2320 | 4.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2594 | 3.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1865 | 3.0 | 12 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2383 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1969 | 3.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 362 | 3.0 | 12 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2574 | 3.0 | 11 |

**go-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2433 | 3.0 | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2379 | 3.0 | 18 |
| `glm-5.1:cloud` | PASS | 2 | 1951 | 2.0 | 18 |
| `kimi-k2.6:cloud` | PASS | 2 | 2173 | 3.0 | 18 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 1936 | 1.0 | 30 |
| `minimax-m3:cloud` | PASS | 2 | 592 | 3.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 2589 | 2.0 | 19 |

**go-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2287 | 4.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2294 | 4.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1858 | 4.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2138 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1705 | 5.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 247 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 3008 | 3.0 | 16 |

**go-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2474 | 2.0 | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2487 | 2.0 | 23 |
| `glm-5.1:cloud` | PASS | 2 | 2006 | 2.0 | 18 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 2265 | 2.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 2033 | 2.0 | 22 |
| `minimax-m3:cloud` | tests-fail | 2 | 323 | 1.0 | 25 |
| `nemotron-3-super:cloud` | PASS | 2 | 2720 | 2.0 | 28 |

**go-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2333 | 4.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2399 | 4.0 | 24 |
| `glm-5.1:cloud` | PASS | 2 | 1933 | 4.0 | 20 |
| `kimi-k2.6:cloud` | PASS | 2 | 2284 | 3.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 2143 | 3.0 | 34 |
| `minimax-m3:cloud` | PASS | 2 | 292 | 4.0 | 37 |
| `nemotron-3-super:cloud` | PASS | 2 | 2928 | 4.0 | 40 |

**go-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2303 | 4.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2307 | 4.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 1860 | 4.0 | 13 |
| `kimi-k2.6:cloud` | ERROR | 4 | 7171 | 4.0 | 20 |
| `minimax-m2.7:cloud` | PASS | 2 | 2070 | 4.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 224 | 3.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2933 | 3.0 | 21 |

**go-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2685 | 2.0 | 33 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2945 | 3.0 | 35 |
| `glm-5.1:cloud` | PASS | 2 | 2103 | 3.0 | 23 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2522 | 3.0 | 26 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2101 | 1.0 | 31 |
| `minimax-m3:cloud` | PASS | 2 | 1409 | 4.0 | 26 |
| `nemotron-3-super:cloud` | PASS | 2 | 4346 | 3.0 | 34 |

**go-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2594 | 3.0 | 25 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2670 | 3.0 | 34 |
| `glm-5.1:cloud` | PASS | 2 | 2182 | 3.0 | 27 |
| `kimi-k2.6:cloud` | PASS | 2 | 2710 | 3.0 | 26 |
| `minimax-m2.7:cloud` | PASS | 2 | 2150 | 2.0 | 33 |
| `minimax-m3:cloud` | PASS | 4 | 721 | 3.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 2975 | 4.0 | 33 |

**go-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2663 | 3.0 | 25 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2597 | 4.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2173 | 3.0 | 26 |
| `kimi-k2.6:cloud` | PASS | 2 | 2454 | 4.0 | 26 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 2085 | 2.0 | 28 |
| `minimax-m3:cloud` | PASS | 2 | 257 | 5.0 | 29 |
| `nemotron-3-super:cloud` | PASS | 2 | 3243 | 3.0 | 34 |

**go-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2290 | 3.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2424 | 3.0 | 20 |
| `glm-5.1:cloud` | PASS | 2 | 1900 | 3.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 1919 | 2.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1771 | 2.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 477 | 4.0 | 20 |
| `nemotron-3-super:cloud` | PASS | 2 | 2696 | 2.0 | 30 |

**go-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2467 | 3.0 | 23 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2247 | 4.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1845 | 4.0 | 11 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2189 | 4.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 2075 | 3.0 | 13 |
| `minimax-m3:cloud` | PASS | 2 | 414 | 4.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2674 | 3.0 | 18 |

**go-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2546 | 4.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2692 | 4.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1965 | 3.0 | 12 |
| `kimi-k2.6:cloud` | PASS | 2 | 3110 | 3.0 | 20 |
| `minimax-m2.7:cloud` | PASS | 2 | 2245 | 3.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 211 | 3.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 3301 | 3.0 | 26 |

**go-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2529 | 2.0 | 28 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2543 | 2.0 | 24 |
| `glm-5.1:cloud` | PASS | 2 | 1993 | 3.0 | 19 |
| `kimi-k2.6:cloud` | PASS | 2 | 2119 | 4.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 2029 | 2.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 621 | 4.0 | 23 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2587 | 2.0 | 21 |

**go-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2405 | 5.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2444 | 4.0 | 22 |
| `glm-5.1:cloud` | PASS | 2 | 2023 | 5.0 | 24 |
| `kimi-k2.6:cloud` | PASS | 2 | 2310 | 5.0 | 24 |
| `minimax-m2.7:cloud` | PASS | 2 | 2143 | 4.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 672 | 4.0 | 22 |
| `nemotron-3-super:cloud` | PASS | 2 | 2642 | 4.0 | 23 |

**go-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2550 | 3.0 | 22 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2553 | 3.0 | 24 |
| `glm-5.1:cloud` | PASS | 2 | 2169 | 4.0 | 30 |
| `kimi-k2.6:cloud` | PASS | 2 | 2650 | 4.0 | 32 |
| `minimax-m2.7:cloud` | PASS | 2 | 1938 | 3.0 | 22 |
| `minimax-m3:cloud` | PASS | 2 | 266 | 3.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 2743 | 3.0 | 26 |

**go-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2474 | 4.0 | 25 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2451 | 3.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 1953 | 4.0 | 19 |
| `kimi-k2.6:cloud` | PASS | 2 | 2239 | 5.0 | 18 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 1880 | 1.0 | 25 |
| `minimax-m3:cloud` | PASS | 2 | 227 | 3.0 | 19 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2672 | 5.0 | 28 |

**go-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2592 | 2.0 | 22 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2633 | 3.0 | 24 |
| `glm-5.1:cloud` | PASS | 2 | 2098 | 3.0 | 23 |
| `kimi-k2.6:cloud` | ERROR | 2 | 3062 | 4.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 2005 | 2.0 | 22 |
| `minimax-m3:cloud` | PASS | 2 | 327 | 4.0 | 36 |
| `nemotron-3-super:cloud` | PASS | 2 | 2929 | 3.0 | 44 |

**go-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2637 | 3.0 | 27 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2663 | 4.0 | 29 |
| `glm-5.1:cloud` | PASS | 2 | 2183 | 4.0 | 30 |
| `kimi-k2.6:cloud` | PASS | 2 | 11278 | 3.0 | 29 |
| `minimax-m2.7:cloud` | PASS | 2 | 2046 | 2.0 | 28 |
| `minimax-m3:cloud` | PASS | 2 | 336 | 4.0 | 36 |
| `nemotron-3-super:cloud` | PASS | 2 | 3130 | 4.0 | 31 |

**go-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2530 | 2.0 | 22 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2514 | 2.0 | 21 |
| `glm-5.1:cloud` | PASS | 2 | 2109 | 2.0 | 22 |
| `kimi-k2.6:cloud` | PASS | 2 | 2427 | 4.0 | 19 |
| `minimax-m2.7:cloud` | PASS | 2 | 2079 | 3.0 | 23 |
| `minimax-m3:cloud` | PASS | 2 | 352 | 3.0 | 39 |
| `nemotron-3-super:cloud` | PASS | 2 | 3011 | 3.0 | 27 |

**go-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2376 | 2.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2379 | 2.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 1914 | 3.0 | 17 |
| `kimi-k2.6:cloud` | PASS | 2 | 2303 | 3.0 | 18 |
| `minimax-m2.7:cloud` | tests-fail | 2 | 1780 | 1.0 | 20 |
| `minimax-m3:cloud` | PASS | 2 | 236 | 4.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2670 | 3.0 | 26 |

**python-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2286 | 4.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2297 | 4.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1848 | 5.0 | 1 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1871 | 5.0 | 2 |
| `minimax-m2.7:cloud` | PASS | 2 | 1819 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 105 | 5.0 | 2 |
| `nemotron-3-super:cloud` | PASS | 2 | 2491 | 4.0 | 10 |

**python-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2228 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2219 | 4.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1820 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 2298 | 5.0 | 2 |
| `minimax-m2.7:cloud` | PASS | 2 | 1552 | 4.0 | 2 |
| `minimax-m3:cloud` | PASS | 2 | 117 | 4.0 | 5 |
| `nemotron-3-super:cloud` | PASS | 2 | 2780 | 3.0 | 9 |

**python-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2302 | 4.0 | 5 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2284 | 5.0 | 5 |
| `glm-5.1:cloud` | PASS | 2 | 1862 | 4.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1932 | 5.0 | 5 |
| `minimax-m2.7:cloud` | PASS | 2 | 2034 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 121 | 5.0 | 3 |
| `nemotron-3-super:cloud` | PASS | 2 | 2436 | 5.0 | 8 |

**python-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2230 | 4.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2249 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1802 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1772 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1620 | 4.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 115 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2511 | 4.0 | 9 |

**python-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2222 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2235 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1805 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1732 | 5.0 | 3 |
| `minimax-m2.7:cloud` | PASS | 2 | 1765 | 5.0 | 2 |
| `minimax-m3:cloud` | PASS | 2 | 113 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2404 | 4.0 | 9 |

**python-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2277 | 4.0 | 6 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2223 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1808 | 5.0 | 3 |
| `kimi-k2.6:cloud` | PASS | 2 | 1718 | 5.0 | 2 |
| `minimax-m2.7:cloud` | PASS | 2 | 1649 | 5.0 | 4 |
| `minimax-m3:cloud` | PASS | 2 | 130 | 4.0 | 6 |
| `nemotron-3-super:cloud` | PASS | 2 | 2544 | 4.0 | 12 |

**python-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2247 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2251 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1797 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1849 | 5.0 | 3 |
| `minimax-m2.7:cloud` | PASS | 2 | 1779 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 179 | 3.0 | 5 |
| `nemotron-3-super:cloud` | PASS | 2 | 2814 | 3.0 | 11 |

**python-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2651 | 4.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2758 | 4.0 | 16 |
| `glm-5.1:cloud` | PASS | 2 | 2207 | 4.0 | 10 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2209 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2157 | 4.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 264 | 4.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 3000 | 5.0 | 19 |

**python-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2348 | 5.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2268 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1796 | 5.0 | 3 |
| `kimi-k2.6:cloud` | PASS | 2 | 1727 | 5.0 | 3 |
| `minimax-m2.7:cloud` | PASS | 2 | 1614 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 185 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2421 | 5.0 | 11 |

**python-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2399 | 5.0 | 6 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2495 | 5.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 1904 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 2173 | 4.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1980 | 4.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 157 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2607 | 3.0 | 18 |

**python-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2219 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2232 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1775 | 5.0 | 2 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1773 | 5.0 | 5 |
| `minimax-m2.7:cloud` | PASS | 2 | 1640 | 5.0 | 4 |
| `minimax-m3:cloud` | PASS | 2 | 106 | 5.0 | 3 |
| `nemotron-3-super:cloud` | PASS | 2 | 2616 | 5.0 | 7 |

**python-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2215 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2244 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1826 | 5.0 | 2 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1763 | 5.0 | 1 |
| `minimax-m2.7:cloud` | PASS | 2 | 1598 | 5.0 | 2 |
| `minimax-m3:cloud` | PASS | 2 | 166 | 5.0 | 6 |
| `nemotron-3-super:cloud` | PASS | 2 | 2668 | 4.0 | 7 |

**python-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2267 | 5.0 | 4 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2381 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1819 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1885 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1746 | 5.0 | 4 |
| `minimax-m3:cloud` | PASS | 2 | 134 | 5.0 | 4 |
| `nemotron-3-super:cloud` | PASS | 2 | 2569 | 5.0 | 10 |

**python-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2339 | 4.0 | 6 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2303 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1856 | 4.0 | 3 |
| `kimi-k2.6:cloud` | PASS | 2 | 1802 | 5.0 | 3 |
| `minimax-m2.7:cloud` | PASS | 2 | 1725 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 131 | 5.0 | 3 |
| `nemotron-3-super:cloud` | PASS | 2 | 2497 | 4.0 | 10 |

**python-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2302 | 4.0 | 13 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2316 | 4.0 | 13 |
| `glm-5.1:cloud` | PASS | 2 | 1926 | 5.0 | 11 |
| `kimi-k2.6:cloud` | PASS | 2 | 2122 | 5.0 | 16 |
| `minimax-m2.7:cloud` | PASS | 2 | 1915 | 4.0 | 15 |
| `minimax-m3:cloud` | PASS | 2 | 257 | 5.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2706 | 4.0 | 23 |

**python-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2219 | 5.0 | 2 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2201 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1786 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1899 | 3.0 | 2 |
| `minimax-m2.7:cloud` | PASS | 2 | 1601 | 4.0 | 2 |
| `minimax-m3:cloud` | PASS | 2 | 113 | 4.0 | 2 |
| `nemotron-3-super:cloud` | PASS | 2 | 2398 | 4.0 | 6 |

**python-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2309 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2339 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1925 | 4.0 | 4 |
| `kimi-k2.6:cloud` | PASS | 2 | 2017 | 4.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1908 | 4.0 | 4 |
| `minimax-m3:cloud` | PASS | 2 | 164 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2457 | 4.0 | 10 |

**python-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2358 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2352 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1919 | 5.0 | 3 |
| `kimi-k2.6:cloud` | PASS | 2 | 2002 | 5.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 1767 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 154 | 4.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2520 | 4.0 | 10 |

**python-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2445 | 4.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2471 | 4.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 2016 | 5.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2374 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1839 | 5.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 169 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2678 | 4.0 | 16 |

**python-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2200 | 5.0 | 2 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2248 | 5.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 1773 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1716 | 5.0 | 2 |
| `minimax-m2.7:cloud` | PASS | 2 | 1581 | 5.0 | 2 |
| `minimax-m3:cloud` | PASS | 2 | 118 | 5.0 | 6 |
| `nemotron-3-super:cloud` | PASS | 2 | 2434 | 5.0 | 6 |

**python-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2269 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2258 | 4.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1778 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1899 | 5.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 1707 | 5.0 | 2 |
| `minimax-m3:cloud` | PASS | 2 | 244 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2956 | 5.0 | 9 |

**python-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2272 | 5.0 | 4 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2342 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1820 | 5.0 | 2 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1871 | 4.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 1678 | 4.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 132 | 4.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2459 | 4.0 | 14 |

**python-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2312 | 5.0 | 4 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2322 | 4.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1868 | 4.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 2141 | 5.0 | 6 |
| `minimax-m2.7:cloud` | PASS | 2 | 1809 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 205 | 4.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2489 | 4.0 | 11 |

**python-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2319 | 3.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2267 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1833 | 5.0 | 3 |
| `kimi-k2.6:cloud` | PASS | 2 | 2016 | 4.0 | 2 |
| `minimax-m2.7:cloud` | PASS | 2 | 1796 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 396 | 4.0 | 3 |
| `nemotron-3-super:cloud` | PASS | 2 | 2499 | 5.0 | 12 |

**python-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2535 | 5.0 | 4 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2335 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1950 | 5.0 | 5 |
| `kimi-k2.6:cloud` | PASS | 2 | 2051 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1728 | 3.0 | 4 |
| `minimax-m3:cloud` | PASS | 2 | 151 | 5.0 | 4 |
| `nemotron-3-super:cloud` | PASS | 2 | 2681 | 5.0 | 11 |

**python-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2287 | 4.0 | 6 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2269 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1825 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1914 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1641 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 141 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2471 | 5.0 | 11 |

**python-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2395 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2444 | 4.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1993 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 2158 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1789 | 3.0 | 4 |
| `minimax-m3:cloud` | PASS | 2 | 189 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2602 | 4.0 | 14 |

**python-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2418 | 4.0 | 10 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2442 | 5.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1947 | 5.0 | 4 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1982 | 5.0 | 4 |
| `minimax-m2.7:cloud` | PASS | 2 | 1797 | 2.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 168 | 5.0 | 4 |
| `nemotron-3-super:cloud` | PASS | 2 | 2712 | 4.0 | 13 |

**python-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2340 | 5.0 | 4 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2321 | 5.0 | 4 |
| `glm-5.1:cloud` | PASS | 2 | 1884 | 5.0 | 4 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1746 | 5.0 | 4 |
| `minimax-m2.7:cloud` | PASS | 2 | 1851 | 5.0 | 4 |
| `minimax-m3:cloud` | PASS | 2 | 194 | 5.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2539 | 5.0 | 11 |

**python-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2212 | 5.0 | 3 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2224 | 5.0 | 3 |
| `glm-5.1:cloud` | PASS | 2 | 1778 | 5.0 | 2 |
| `kimi-k2.6:cloud` | ERROR | 2 | 1728 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1585 | 5.0 | 3 |
| `minimax-m3:cloud` | PASS | 2 | 136 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2442 | 4.0 | 11 |

**rust-easy-01-sum-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2443 | 5.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2432 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1979 | 5.0 | 9 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 2007 | 4.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 2021 | 5.0 | 10 |
| `minimax-m3:cloud` | PASS | 2 | 231 | 5.0 | 10 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2700 | 5.0 | 10 |

**rust-easy-02-reverse-string**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2417 | 4.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2377 | 3.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1883 | 4.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 2148 | 5.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1703 | 3.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 367 | 4.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 3030 | 4.0 | 14 |

**rust-easy-03-count-vowels**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2454 | 4.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2441 | 5.0 | 10 |
| `glm-5.1:cloud` | PASS | 2 | 1943 | 5.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2133 | 3.0 | 15 |
| `minimax-m2.7:cloud` | PASS | 2 | 1763 | 3.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 261 | 5.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2746 | 5.0 | 7 |

**rust-easy-04-max-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 5 | 8302 | 3.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2367 | 4.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1920 | 5.0 | 10 |
| `kimi-k2.6:cloud` | tests-fail | 2 | 2313 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1873 | 3.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 307 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2509 | 5.0 | 11 |

**rust-easy-05-min-of-list**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2451 | 3.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2397 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1909 | 4.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2238 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1928 | 3.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 251 | 3.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 2464 | 5.0 | 11 |

**rust-easy-06-factorial**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2362 | 4.0 | 8 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2393 | 3.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 2046 | 4.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 1939 | 3.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1981 | 4.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 233 | 4.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 2648 | 4.0 | 11 |

**rust-easy-07-is-palindrome**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2402 | 3.0 | 18 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2367 | 3.0 | 14 |
| `glm-5.1:cloud` | PASS | 2 | 1949 | 3.0 | 11 |
| `kimi-k2.6:cloud` | PASS | 2 | 2138 | 4.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1913 | 3.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 318 | 4.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2715 | 5.0 | 9 |

**rust-easy-08-fizzbuzz**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2776 | 4.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2804 | 4.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2392 | 4.0 | 18 |
| `kimi-k2.6:cloud` | PASS | 2 | 2581 | 4.0 | 20 |
| `minimax-m2.7:cloud` | PASS | 2 | 2484 | 4.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 552 | 4.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2896 | 4.0 | 17 |

**rust-easy-09-gcd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2437 | 3.0 | 16 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2491 | 4.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2007 | 3.0 | 16 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2305 | 4.0 | 17 |
| `minimax-m2.7:cloud` | PASS | 2 | 2182 | 4.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 522 | 4.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 3688 | 4.0 | 17 |

**rust-easy-10-nth-fibonacci**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2615 | 3.0 | 18 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2646 | 4.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2066 | 3.0 | 15 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2767 | 4.0 | 14 |
| `minimax-m2.7:cloud` | PASS | 2 | 1960 | 3.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 315 | 4.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 3680 | 5.0 | 14 |

**rust-easy-11-count-words**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2320 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2285 | 5.0 | 7 |
| `glm-5.1:cloud` | PASS | 2 | 1874 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 1804 | 5.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1793 | 5.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 153 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2449 | 5.0 | 7 |

**rust-easy-12-sum-digits**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2439 | 4.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2362 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1915 | 4.0 | 6 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2013 | 5.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 2025 | 4.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 171 | 4.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2573 | 5.0 | 12 |

**rust-easy-13-celsius-to-fahrenheit**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2402 | 4.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2419 | 3.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1913 | 4.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2193 | 4.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1966 | 4.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 579 | 4.0 | 8 |
| `nemotron-3-super:cloud` | PASS | 2 | 2592 | 4.0 | 8 |

**rust-easy-14-average**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2523 | 2.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2476 | 3.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 2004 | 3.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2078 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1918 | 3.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 247 | 4.0 | 13 |
| `nemotron-3-super:cloud` | PASS | 2 | 2722 | 3.0 | 13 |

**rust-easy-15-is-prime**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2548 | 4.0 | 24 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2573 | 3.0 | 27 |
| `glm-5.1:cloud` | PASS | 2 | 2114 | 3.0 | 17 |
| `kimi-k2.6:cloud` | PASS | 2 | 2638 | 3.0 | 30 |
| `minimax-m2.7:cloud` | PASS | 2 | 2010 | 3.0 | 30 |
| `minimax-m3:cloud` | PASS | 2 | 344 | 3.0 | 26 |
| `nemotron-3-super:cloud` | PASS | 2 | 2947 | 3.0 | 31 |

**rust-easy-16-to-uppercase**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2292 | 5.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2289 | 3.0 | 5 |
| `glm-5.1:cloud` | PASS | 2 | 1886 | 4.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 3614 | 4.0 | 7 |
| `minimax-m2.7:cloud` | PASS | 2 | 1678 | 3.0 | 7 |
| `minimax-m3:cloud` | PASS | 2 | 195 | 3.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2743 | 3.0 | 9 |

**rust-easy-17-second-largest**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2588 | 3.0 | 20 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2533 | 4.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 2081 | 5.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2400 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2339 | 4.0 | 14 |
| `minimax-m3:cloud` | PASS | 2 | 587 | 3.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2876 | 3.0 | 14 |

**rust-easy-18-sort-ascending**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2600 | 3.0 | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2541 | 4.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 2065 | 4.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2212 | 3.0 | 16 |
| `minimax-m2.7:cloud` | PASS | 2 | 1983 | 4.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 247 | 5.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2710 | 5.0 | 16 |

**rust-easy-19-dedupe-order**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2693 | 2.0 | 24 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2859 | 3.0 | 17 |
| `glm-5.1:cloud` | PASS | 2 | 2189 | 4.0 | 16 |
| `kimi-k2.6:cloud` | PASS | 2 | 3089 | 3.0 | 24 |
| `minimax-m2.7:cloud` | PASS | 2 | 2195 | 4.0 | 21 |
| `minimax-m3:cloud` | PASS | 2 | 292 | 4.0 | 19 |
| `nemotron-3-super:cloud` | PASS | 2 | 2794 | 4.0 | 22 |

**rust-easy-20-binary-to-decimal**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2320 | 3.0 | 7 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2305 | 4.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1901 | 5.0 | 8 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2590 | 4.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 2083 | 2.0 | 8 |
| `minimax-m3:cloud` | PASS | 2 | 419 | 3.0 | 15 |
| `nemotron-3-super:cloud` | PASS | 2 | 2585 | 4.0 | 8 |

**rust-easy-21-decimal-to-binary**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2359 | 4.0 | 11 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2331 | 3.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1898 | 3.0 | 11 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2055 | 3.0 | 8 |
| `minimax-m2.7:cloud` | PASS | 2 | 1957 | 3.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 409 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2632 | 4.0 | 7 |

**rust-easy-22-power**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2461 | 3.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2534 | 4.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 2009 | 2.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 4316 | 4.0 | 9 |
| `minimax-m2.7:cloud` | PASS | 2 | 2218 | 4.0 | 9 |
| `minimax-m3:cloud` | PASS | 2 | 210 | 4.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2891 | 3.0 | 10 |

**rust-easy-23-sum-even**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2498 | 3.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2455 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1994 | 5.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2345 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2154 | 5.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 260 | 5.0 | 11 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2546 | 5.0 | 11 |

**rust-easy-24-longest-word**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | tests-fail | 2 | 2434 | 4.0 | 12 |
| `deepseek-v4-pro:cloud` | tests-fail | 2 | 2397 | 2.0 | 11 |
| `glm-5.1:cloud` | tests-fail | 2 | 1919 | — | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 2180 | 4.0 | 12 |
| `minimax-m2.7:cloud` | PASS | 2 | 1827 | 3.0 | 13 |
| `minimax-m3:cloud` | tests-fail | 2 | 325 | 2.0 | 10 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 2727 | 3.0 | 8 |

**rust-easy-25-title-case**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2556 | 5.0 | 17 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2601 | 1.0 | 20 |
| `glm-5.1:cloud` | PASS | 2 | 2085 | 5.0 | 14 |
| `kimi-k2.6:cloud` | PASS | 2 | 3252 | 4.0 | 21 |
| `minimax-m2.7:cloud` | PASS | 2 | 2164 | 1.0 | 19 |
| `minimax-m3:cloud` | PASS | 2 | 1171 | 4.0 | 25 |
| `nemotron-3-super:cloud` | tests-fail | 2 | 3064 | 3.0 | 20 |

**rust-easy-26-count-evens**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2430 | 3.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2414 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1952 | 5.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 2077 | 3.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 1793 | 5.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 236 | 4.0 | 10 |
| `nemotron-3-super:cloud` | PASS | 2 | 2759 | 5.0 | 11 |

**rust-easy-27-prefix-sums**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2670 | 3.0 | 21 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2640 | 2.0 | 24 |
| `glm-5.1:cloud` | PASS | 2 | 2113 | 3.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2237 | 4.0 | 18 |
| `minimax-m2.7:cloud` | PASS | 2 | 2275 | 3.0 | 19 |
| `minimax-m3:cloud` | PASS | 2 | 641 | 4.0 | 18 |
| `nemotron-3-super:cloud` | PASS | 2 | 2842 | 3.0 | 23 |

**rust-easy-28-is-anagram**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2538 | 4.0 | 12 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2670 | 4.0 | 23 |
| `glm-5.1:cloud` | PASS | 2 | 2086 | 4.0 | 12 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2596 | 4.0 | 10 |
| `minimax-m2.7:cloud` | PASS | 2 | 2022 | 4.0 | 17 |
| `minimax-m3:cloud` | PASS | 2 | 511 | 4.0 | 30 |
| `nemotron-3-super:cloud` | PASS | 2 | 3134 | 3.0 | 21 |

**rust-easy-29-median-odd**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2470 | 2.0 | 14 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2480 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1997 | 3.0 | 8 |
| `kimi-k2.6:cloud` | ERROR | 2 | 2071 | 5.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 2160 | 3.0 | 12 |
| `minimax-m3:cloud` | PASS | 2 | 322 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2641 | 4.0 | 14 |

**rust-easy-30-sum-of-squares**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2398 | 3.0 | 15 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2374 | 5.0 | 12 |
| `glm-5.1:cloud` | PASS | 2 | 1904 | 5.0 | 10 |
| `kimi-k2.6:cloud` | PASS | 2 | 1950 | 4.0 | 11 |
| `minimax-m2.7:cloud` | PASS | 2 | 1758 | 5.0 | 11 |
| `minimax-m3:cloud` | PASS | 2 | 420 | 5.0 | 11 |
| `nemotron-3-super:cloud` | PASS | 2 | 2550 | 5.0 | 11 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models glm-5.1:cloud,deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud,minimax-m3:cloud,nemotron-3-super:cloud,kimi-k2.6:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`76440a746bdd` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
