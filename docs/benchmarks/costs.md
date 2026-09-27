# Benchmark costs

> ↟ [Benchmark index](index.md)

Prices: [https://ollama.com/pricing](https://ollama.com/pricing) snapshot of **2026-09-23** (`benchmarks/consultants/pricing.py`). Prompt tokens are priced uncached, because the traces do not record cache hits, so every figure is an upper bound. A model with no row on the pricing page is listed as unpriced and the total is marked *(floor)*.

## Ladders — what quality costs

Every coder_med model that was measured, is still offered and has a price: 9 models, 60 trials each, all judged by kimi-k2.6. Reference run `benchmarks/consultants/results/2026-09-23/coder_med`. Prices: snapshot of **2026-09-23**, list (peak) rate, subject spend only (the judge does not run in production). Retiring models are left out: `deepseek-v4-flash` (2026-09-25). Method: `scripts/bench_ladders.py`.

† from `benchmarks/consultants/results/2026-06-04/coder_med`, calibrated on 2 models measured in both runs (`deepseek-v4-pro:cloud`, `minimax-m3:cloud`): Q × 1.224, seconds × 0.106. 2 anchor(s) make this an estimate: a † rung says where to re-run, not what the model measures today.

**Q** = mean of *judge score ÷ 5* over trials whose tests pass (failing trials count 0) — working code, weighted by how good it is. ⚠ = below the skill-eval bar (pass ≥ 70%, mean judge score ≥ 3.5).

### Value ladder — cost / quality

Ranked by **$ per quality point** (mean $ per trial ÷ Q): what one unit of delivered quality costs. Lower is better.

| # | Model | Q | Pass | Judge | $ / trial | **$ / Q point** | off-peak $ / Q |
|---|---|---|---|---|---|---|---|
| 1 | `glm-5.3-flash:cloud` | 0.770 | 97% | 3.88 | $0.0005 | **$0.0007** | $0.0007 |
| 2 | `nemotron-3-super:cloud` † | 0.714 | 88% | 3.62 | $0.0006 | **$0.0009** | $0.0009 |
| 3 | `deepseek-v4.1-flash:cloud` | 0.827 | 100% | 4.13 | $0.0013 | **$0.0016** | $0.0008 |
| 4 | `minimax-m2.7:cloud` † ⚠ below bar | 0.628 | 77% | 3.30 | $0.0019 | **$0.0030** | $0.0030 |
| 5 | `glm-5.1:cloud` † | 0.747 | 95% | 3.85 | $0.0039 | **$0.0052** | $0.0052 |
| 6 | `minimax-m3:cloud` | 0.813 | 97% | 4.10 | $0.0044 | **$0.0054** | $0.0054 |
| 7 | `glm-5.3:cloud` | 0.723 | 92% | 3.86 | $0.0049 | **$0.0068** | $0.0068 |
| 8 | `kimi-k2.6:cloud` † | 0.906 | 100% | 4.19 | $0.0080 | **$0.0089** | $0.0089 |
| 9 | `deepseek-v4-pro:cloud` | 0.833 | 98% | 4.25 | $0.0075 | **$0.0090** | $0.0045 |

### Throughput ladder — cost / quality / speed

Ranked by **Q ÷ √(cost_rel × time_rel)**, cost and time each relative to the field median; indexed so the median model is 100. Cost and speed weigh equally. Higher is better.

| # | Model | Q | $ / trial | cost_rel | median s | p90 s | time_rel | **index** |
|---|---|---|---|---|---|---|---|---|
| 1 | `glm-5.3-flash:cloud` | 0.770 | $0.0005 | 0.14 | 3.8 | 8.5 | 0.88 | **273** |
| 2 | `deepseek-v4.1-flash:cloud` | 0.827 | $0.0013 | 0.35 | 2.1 | 3.6 | 0.49 | **252** |
| 3 | `nemotron-3-super:cloud` † | 0.714 | $0.0006 | 0.16 | 4.1 | 7.3 | 0.95 | **228** |
| 4 | `glm-5.3:cloud` | 0.723 | $0.0049 | 1.27 | 2.4 | 11.1 | 0.56 | **107** |
| 5 | `minimax-m2.7:cloud` † ⚠ below bar | 0.628 | $0.0019 | 0.48 | 5.5 | 9.6 | 1.28 | **100** |
| 6 | `glm-5.1:cloud` † | 0.747 | $0.0039 | 1.00 | 4.3 | 8.1 | 1.00 | **93** |
| 7 | `minimax-m3:cloud` | 0.813 | $0.0044 | 1.15 | 5.8 | 23.4 | 1.34 | **82** |
| 8 | `kimi-k2.6:cloud` † | 0.906 | $0.0080 | 2.09 | 5.1 | 8.4 | 1.18 | **72** |
| 9 | `deepseek-v4-pro:cloud` | 0.833 | $0.0075 | 1.95 | 6.2 | 25.6 | 1.43 | **63** |


## Council-role sweeps

Per run directory: every role's tokens from `turns`, priced per role's model at the time the query ran (peak/off-peak).

| Label | smoke | audit-medium | audit-high | Total | Prompt tok | Completion tok | Unpriced models |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-1-flash-cloud-2026-09-23-screening` | $0.0451 | $0.0822 | $0.0667 | $0.1939 | 532,809 | 28,409 |  |
| `deepseek-v4-flash-cloud-2026-05-09-r2` | $0.0163 | $0.0604 | $0.0534 | $0.1301 | 494,196 | 32,442 |  |
| `deepseek-v4-flash-cloud-2026-05-09-r3` | $0.0189 | $0.0462 | $0.0519 | $0.1170 | 430,247 | 33,909 |  |
| `deepseek-v4-flash-cloud-2026-05-09-screening` | $0.0188 | $0.0442 | $0.0477 | $0.1108 | 414,202 | 29,798 |  |
| `deepseek-v4-pro-cloud-2026-05-09-screening` | $0.0582 | $0.2620 | $0.1334 | $0.4536 | 596,769 | 30,188 |  |
| `deepseek-v4-pro-cloud-2026-09-23-screening` | $0.0930 | $0.3147 | $0.2564 | $0.6641 | 406,602 | 32,175 |  |
| `gemini-3-flash-preview-cloud-2026-05-09-r2` | unpriced | unpriced | unpriced | unpriced | 617,485 | 34,462 | gemini-3-flash-preview:cloud |
| `gemini-3-flash-preview-cloud-2026-05-09-r3` | unpriced | unpriced | unpriced | unpriced | 476,707 | 31,435 | gemini-3-flash-preview:cloud |
| `gemini-3-flash-preview-cloud-2026-05-09-screening` | unpriced | unpriced | unpriced | unpriced | 513,243 | 33,107 | gemini-3-flash-preview:cloud |
| `gemma4-31b-cloud-2026-05-07` | $0.0141 | $0.0179 | $0.0248 | $0.0568 | 369,868 | 12,472 |  |
| `gemma4-31b-cloud-2026-05-09-r1` | $0.0154 | $0.0174 | $0.0216 | $0.0543 | 353,662 | 12,044 |  |
| `gemma4-31b-cloud-2026-05-09-r2` | $0.0155 | $0.0310 | $0.0238 | $0.0703 | 471,519 | 10,630 |  |
| `gemma4-31b-cloud-2026-05-09-r3` | $0.0166 | $0.0266 | $0.0274 | $0.0706 | 464,501 | 13,859 |  |
| `gemma4-31b-cloud-2026-09-23-r1` | $0.0068 | $0.0214 | $0.0165 | $0.0447 | 305,974 | 4,623 |  |
| `gemma4-31b-cloud-2026-09-23-r2` | $0.0068 | $0.0166 | $0.0167 | $0.0401 | 273,558 | 4,521 |  |
| `gemma4-31b-cloud-2026-09-23-r3` | $0.0068 | $0.0177 | $0.0147 | $0.0392 | 266,024 | 4,989 |  |
| `glm-5-1-cloud-2026-05-07` | $0.1122 | $0.1232 | $0.1821 | $0.4175 | 365,043 | 16,390 |  |
| `glm-5-3-cloud-2026-09-23-screening` | $0.0722 | $0.2290 | $0.2021 | $0.5033 | 296,116 | 20,164 |  |
| `glm-5-3-flash-cloud-2026-09-23-r1` | $0.0108 | $0.0222 | $0.0194 | $0.0524 | 280,556 | 20,642 |  |
| `glm-5-3-flash-cloud-2026-09-23-r2` | $0.0043 | $0.0267 | $0.0193 | $0.0502 | 276,723 | 17,446 |  |
| `glm-5-3-flash-cloud-2026-09-23-r3` | $0.0084 | $0.0231 | $0.0217 | $0.0532 | 285,587 | 20,767 |  |
| `glm-5-3-flash-cloud-2026-09-23-screening` | $0.0075 | $0.0209 | $0.0233 | $0.0517 | 279,755 | 19,466 |  |
| `kimi-k2.6-cloud-2026-05-07` | $0.0790 | $0.1077 | $0.2443 | $0.4310 | 240,508 | 50,629 |  |
| `kimi-k2.6-cloud-2026-05-07-pre-harden` | $0.0905 | $0.1797 | $0.3021 | $0.5724 | 339,553 | 62,449 |  |
| `minimax-m2-7-cloud-2026-05-07` | $0.0262 | $0.0326 | $0.1003 | $0.1591 | 424,493 | 26,500 |  |
| `mistral-large-3-675b-cloud-2026-05-09-screening` | $0.0675 | $0.0422 | $0.0709 | $0.1806 | 344,644 | 5,530 |  |
| `mix-gemini-PRC-gemma4-S-2026-05-09-r1` | $0.0002 | $0.0005 | $0.0008 | $0.0016 (floor) | 488,582 | 28,774 | gemini-3-flash-preview:cloud |
| `mix-gemini-PRC-gemma4-S-2026-05-09-r2` | $0.0002 | $0.0005 | $0.0010 | $0.0017 (floor) | 642,735 | 30,690 | gemini-3-flash-preview:cloud |
| `mix-gemini-PRC-gemma4-S-2026-05-09-r3` | $0.0002 | $0.0006 | $0.0010 | $0.0017 (floor) | 422,350 | 28,860 | gemini-3-flash-preview:cloud |
| `nemotron-3-nano-30b-cloud-2026-05-09-screening` | $0.0082 | $0.0102 | $0.0113 | $0.0297 | 423,009 | 18,066 |  |
| `nemotron-3-super-cloud-2026-05-09-r2` | $0.0029 | $0.0087 | $0.0097 | $0.0213 | 424,444 | 24,898 |  |
| `nemotron-3-super-cloud-2026-05-09-r3` | $0.0032 | $0.0062 | $0.0489 | $0.0582 | 549,892 | 83,255 |  |
| `nemotron-3-super-cloud-2026-05-09-screening` | $0.0036 | $0.0065 | $0.0116 | $0.0218 | 428,780 | 25,533 |  |
| `qwen3-5-397b-cloud-2026-05-07` | $0.0540 | $0.0877 | $0.1224 | $0.2641 | 279,354 | 26,808 |  |
| `qwen3-5-cloud-2026-05-07` | unpriced | unpriced | unpriced | unpriced | 288,163 | 31,837 | qwen3.5:cloud |
| `qwen3-coder-next-cloud-2026-05-09-screening` | unpriced | unpriced | unpriced | unpriced | 430,063 | 6,052 | qwen3-coder-next:cloud |

## Skill-eval suites

Judge spend before 2026-09-23 was not recorded by the harness (bug-925): those totals are the coder's spend only.

Skipped 7 `*-aborted-*` run dir(s): partial runs that were never published.

### `benchmarks/consultants/results/2026-05-16/coder`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `qwen3-coder-next:cloud` | 8 | 8 | unpriced | not recorded | unpriced | unpriced | unpriced |
| `gemma4:31b-cloud` | 8 | 8 | $0.0031 | not recorded | $0.0031 (floor) | $0.0003 | $0.0004 |
| `glm-5.1:cloud` | 8 | 8 | $0.0180 | not recorded | $0.0180 (floor) | $0.0022 | $0.0023 |
| `kimi-k2.6:cloud` | 8 | 8 | $0.0311 | not recorded | $0.0311 (floor) | $0.0033 | $0.0039 |

### `benchmarks/consultants/results/2026-05-16/coder-5model`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 1 | 1 | $0.0006 | not recorded | $0.0006 (floor) | $0.0006 | $0.0006 |
| `glm-5.1:cloud` | 1 | 1 | $0.0020 | not recorded | $0.0020 (floor) | $0.0020 | $0.0020 |
| `kimi-k2.6:cloud` | 1 | 1 | $0.0027 | not recorded | $0.0027 (floor) | $0.0027 | $0.0027 |

### `benchmarks/consultants/results/2026-05-16/coder-newmodel-smoke`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 2 | 2 | $0.0012 | not recorded | $0.0012 (floor) | $0.0006 | $0.0006 |
| `minimax-m2.7:cloud` | 2 | 2 | $0.0015 | not recorded | $0.0015 (floor) | $0.0007 | $0.0007 |
| `deepseek-v4-pro:cloud` | 2 | 2 | $0.0034 | not recorded | $0.0034 (floor) | $0.0017 | $0.0017 |

### `benchmarks/consultants/results/2026-05-16/coder_mlang`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 18 | 6 | $0.0223 | not recorded | $0.0223 (floor) | $0.0010 | $0.0037 |
| `minimax-m2.7:cloud` | 18 | 4 | $0.0409 | not recorded | $0.0409 (floor) | $0.0020 | $0.0102 |
| `deepseek-v4-pro:cloud` | 18 | 6 | $0.0922 | not recorded | $0.0922 (floor) | $0.0032 | $0.0154 |
| `glm-5.1:cloud` | 18 | 5 | $0.1820 | not recorded | $0.1820 (floor) | $0.0047 | $0.0364 |
| `kimi-k2.6:cloud` | 18 | 6 | $0.3904 | not recorded | $0.3904 (floor) | $0.0145 | $0.0651 |

### `benchmarks/consultants/results/2026-05-17/coder_mlang-v1.0.1`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 13 | 1 | $0.0189 | not recorded | $0.0189 (floor) | $0.0013 | $0.0189 |
| `minimax-m2.7:cloud` | 13 | 2 | $0.0361 | not recorded | $0.0361 (floor) | $0.0025 | $0.0180 |
| `deepseek-v4-pro:cloud` | 13 | 1 | $0.0770 | not recorded | $0.0770 (floor) | $0.0034 | $0.0770 |
| `glm-5.1:cloud` | 13 | 1 | $0.0960 | not recorded | $0.0960 (floor) | $0.0057 | $0.0960 |
| `kimi-k2.6:cloud` | 13 | 2 | $0.2458 | not recorded | $0.2458 (floor) | $0.0126 | $0.1229 |

### `benchmarks/consultants/results/2026-05-17/stall-tier1`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `glm-5.1:cloud` | 12 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |
| `kimi-k2.6:cloud` | 12 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |
| `gemma4:31b-cloud` | 12 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |
| `qwen3-coder-next:cloud` | 12 | 0 | unpriced | $0.0000 | unpriced | unpriced | unpriced |
| `deepseek-v4-pro:cloud` | 12 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |
| `deepseek-v4-flash:cloud` | 12 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |
| `gemini-3-flash-preview:cloud` | 12 | 0 | unpriced | $0.0000 | unpriced | unpriced | unpriced |

### `benchmarks/consultants/results/2026-05-17/tool_executor`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `qwen3-coder-next:cloud` | 8 | 5 | unpriced | not recorded | unpriced | unpriced | unpriced |
| `gemini-3-flash-preview:cloud` | 8 | 6 | unpriced | not recorded | unpriced | unpriced | unpriced |
| `gemma4:31b-cloud` | 8 | 7 | $0.0108 | not recorded | $0.0108 (floor) | $0.0013 | $0.0015 |
| `deepseek-v4-pro:cloud` | 8 | 7 | $0.0558 | not recorded | $0.0558 (floor) | $0.0067 | $0.0080 |
| `glm-5.1:cloud` | 8 | 7 | $0.0619 | not recorded | $0.0619 (floor) | $0.0088 | $0.0088 |
| `kimi-k2.6:cloud` | 8 | 7 | $0.0730 | not recorded | $0.0730 (floor) | $0.0087 | $0.0104 |

### `benchmarks/consultants/results/2026-05-17/tool_executor-smoke`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `glm-5.1:cloud` | 2 | 2 | $0.0162 | not recorded | $0.0162 (floor) | $0.0081 | $0.0081 |

### `benchmarks/consultants/results/2026-06-03/coder-2new`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `nemotron-3-super:cloud` | 8 | 8 | $0.0021 | not recorded | $0.0021 (floor) | $0.0002 | $0.0003 |
| `minimax-m3:cloud` | 8 | 8 | $0.0184 | not recorded | $0.0184 (floor) | $0.0021 | $0.0023 |

### `benchmarks/consultants/results/2026-06-03/coder_easy`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `nemotron-3-super:cloud` | 180 | 168 | $0.1056 | not recorded | $0.1056 (floor) | $0.0003 | $0.0006 |
| `deepseek-v4-flash:cloud` | 180 | 178 | $0.1195 | not recorded | $0.1195 (floor) | $0.0006 | $0.0007 |
| `minimax-m3:cloud` | 180 | 176 | $0.1386 | not recorded | $0.1386 (floor) | $0.0006 | $0.0008 |
| `minimax-m2.7:cloud` | 180 | 174 | $0.1545 | not recorded | $0.1545 (floor) | $0.0008 | $0.0009 |
| `deepseek-v4-pro:cloud` | 180 | 177 | $0.3607 | not recorded | $0.3607 (floor) | $0.0019 | $0.0020 |
| `glm-5.1:cloud` | 180 | 178 | $0.4197 | not recorded | $0.4197 (floor) | $0.0023 | $0.0024 |
| `kimi-k2.6:cloud` | 180 | 177 | $0.7964 | not recorded | $0.7964 (floor) | $0.0036 | $0.0045 |

### `benchmarks/consultants/results/2026-06-03/coder_mlang-2new`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `nemotron-3-super:cloud` | 13 | 3 | $0.0188 | not recorded | $0.0188 (floor) | $0.0011 | $0.0063 |
| `minimax-m3:cloud` | 13 | 4 | $0.2443 | not recorded | $0.2443 (floor) | $0.0114 | $0.0611 |

### `benchmarks/consultants/results/2026-06-04/coder_med`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `nemotron-3-super:cloud` | 60 | 53 | $0.0374 | not recorded | $0.0374 (floor) | $0.0005 | $0.0007 |
| `deepseek-v4-flash:cloud` | 60 | 55 | $0.0558 | not recorded | $0.0558 (floor) | $0.0008 | $0.0010 |
| `minimax-m2.7:cloud` | 60 | 46 | $0.1118 | not recorded | $0.1118 (floor) | $0.0014 | $0.0024 |
| `deepseek-v4-pro:cloud` | 60 | 59 | $0.1791 | not recorded | $0.1791 (floor) | $0.0026 | $0.0030 |
| `glm-5.1:cloud` | 60 | 57 | $0.2311 | not recorded | $0.2311 (floor) | $0.0032 | $0.0041 |
| `minimax-m3:cloud` | 60 | 58 | $0.3354 | not recorded | $0.3354 (floor) | $0.0033 | $0.0058 |
| `kimi-k2.6:cloud` | 60 | 60 | $0.4829 | not recorded | $0.4829 (floor) | $0.0062 | $0.0080 |

### `benchmarks/consultants/results/2026-06-04/coder_med-pilot`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `nemotron-3-super:cloud` | 6 | 5 | $0.0090 | not recorded | $0.0090 (floor) | $0.0011 | $0.0018 |
| `glm-5.1:cloud` | 6 | 5 | $0.0273 | not recorded | $0.0273 (floor) | $0.0039 | $0.0055 |
| `minimax-m3:cloud` | 6 | 6 | $0.0336 | not recorded | $0.0336 (floor) | $0.0053 | $0.0056 |

### `benchmarks/consultants/results/2026-08-01/role-tools-tier1`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `gemma4:31b-cloud` | 36 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |

### `benchmarks/consultants/results/2026-08-01/role-tools-tier1-addendum`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `gemma4:31b-cloud` | 36 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |

### `benchmarks/consultants/results/2026-08-01/role-tools-tier1-v1.1`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `gemma4:31b-cloud` | 36 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |

### `benchmarks/consultants/results/2026-08-01/role-tools-tier1-v1.2`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `gemma4:31b-cloud` | 36 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |

### `benchmarks/consultants/results/2026-08-01/role-tools-tier1-v1.3`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `gemma4:31b-cloud` | 72 | 0 | $0.0000 | $0.0000 | $0.0000 | $0.0000 | — |

### `benchmarks/consultants/results/2026-09-23/coder_med`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `glm-5.3-flash:cloud` | 60 | 58 | $0.0329 | $1.10 | $1.13 | $0.0172 | $0.0195 |
| `deepseek-v4.1-flash:cloud` | 60 | 60 | $0.0399 | $0.9204 | $0.9603 | $0.0154 | $0.0160 |
| `deepseek-v4-pro:cloud` | 60 | 59 | $0.2250 | $0.8223 | $1.05 | $0.0163 | $0.0178 |
| `minimax-m3:cloud` | 60 | 58 | $0.2652 | $0.8781 | $1.14 | $0.0169 | $0.0197 |
| `glm-5.3:cloud` | 60 | 55 | $0.2943 | $0.9175 | $1.21 | $0.0188 | $0.0220 |

### `benchmarks/consultants/results/2026-09-23/coder_med-sampling`

| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial (median) | $/pass |
|---|---|---|---|---|---|---|---|
| `glm-5.3-flash:cloud` arm `glm-5.3-flash-t07-pen` | 60 | 56 | $0.0296 | $1.09 | $1.11 | $0.0179 | $0.0199 |
| `glm-5.3-flash:cloud` arm `glm-5.3-flash-t07` | 60 | 58 | $0.0313 | $1.01 | $1.04 | $0.0154 | $0.0179 |
| `deepseek-v4.1-flash:cloud` arm `deepseek-v4.1-flash-t07` | 60 | 60 | $0.0402 | $0.9283 | $0.9685 | $0.0152 | $0.0161 |

## Judge evaluations

`benchmarks/consultants/judge_eval.py` verdicts (`benchmarks/consultants/results/2026-09-23/judge_eval`, `benchmarks/consultants/results/2026-09-23/judge_eval_june`). Each record is one paid call: failed verdicts and the repairs that replaced them are both counted. A panel row carries its members' calls as well as the synthesizer's, so it overlaps the members' rows. Findings: [`judge-and-sampling.md`](judge-and-sampling.md).

| Judge (label) | Verdicts | Failed | Repaired | Total $ | $/verdict |
|---|---|---|---|---|---|
| `deepseek-v4.1-flash:cloud@temperature=0.7` | 781 | 1 | 1 | $0.2329 | $0.0003 |
| `deepseek-v4.1-flash:cloud` | 827 | 0 | 0 | $0.3531 | $0.0004 |
| `glm-5.3-flash:cloud@temperature=0.7` | 905 | 125 | 125 | $0.4638 | $0.0005 |
| `glm-5.3-flash:cloud@frequency_penalty=0.1,repeat_last_n=2048,repeat_penalty=1.1,temperature=0.7` | 780 | 0 | 0 | $0.4490 | $0.0006 |
| `glm-5.3-flash:cloud` | 828 | 1 | 1 | $0.4945 | $0.0006 |
| `deepseek-v4.1-flash:cloud#panel=glm-5.3-flash:cloud+deepseek-v4.1-flash:cloud` | 780 | 0 | 0 | $1.57 | $0.0020 |
| `kimi-k2.6:cloud` | 71 | 4 | 4 | $1.03 | $0.0145 |

