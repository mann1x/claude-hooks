# Benchmark costs

Prices: [https://ollama.com/pricing](https://ollama.com/pricing) snapshot of **2026-09-23** (`benchmarks/consultants/pricing.py`). Prompt tokens are priced uncached, because the traces do not record cache hits, so every figure is an upper bound. A model with no row on the pricing page is listed as unpriced and the total is marked *(floor)*.

## Council-role sweeps

Per run directory: every role's tokens from `turns`, priced per role's model at the time the query ran (peak/off-peak).

| Label | smoke | audit-medium | audit-high | Total | Prompt tok | Completion tok | Unpriced models |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash-cloud-2026-05-09-r2` | $0.0163 | $0.0604 | $0.0534 | $0.1301 | 494,196 | 32,442 |  |
| `deepseek-v4-flash-cloud-2026-05-09-r3` | $0.0189 | $0.0462 | $0.0519 | $0.1170 | 430,247 | 33,909 |  |
| `deepseek-v4-flash-cloud-2026-05-09-screening` | $0.0188 | $0.0442 | $0.0477 | $0.1108 | 414,202 | 29,798 |  |
| `deepseek-v4-pro-cloud-2026-05-09-screening` | $0.0582 | $0.2620 | $0.1334 | $0.4536 | 596,769 | 30,188 |  |
| `gemini-3-flash-preview-cloud-2026-05-09-r2` | unpriced | unpriced | unpriced | unpriced | 617,485 | 34,462 | gemini-3-flash-preview:cloud |
| `gemini-3-flash-preview-cloud-2026-05-09-r3` | unpriced | unpriced | unpriced | unpriced | 476,707 | 31,435 | gemini-3-flash-preview:cloud |
| `gemini-3-flash-preview-cloud-2026-05-09-screening` | unpriced | unpriced | unpriced | unpriced | 513,243 | 33,107 | gemini-3-flash-preview:cloud |
| `gemma4-31b-cloud-2026-05-07` | $0.0141 | $0.0179 | $0.0248 | $0.0568 | 369,868 | 12,472 |  |
| `gemma4-31b-cloud-2026-05-09-r1` | $0.0154 | $0.0174 | $0.0216 | $0.0543 | 353,662 | 12,044 |  |
| `gemma4-31b-cloud-2026-05-09-r2` | $0.0155 | $0.0310 | $0.0238 | $0.0703 | 471,519 | 10,630 |  |
| `gemma4-31b-cloud-2026-05-09-r3` | $0.0166 | $0.0266 | $0.0274 | $0.0706 | 464,501 | 13,859 |  |
| `glm-5-1-cloud-2026-05-07` | $0.1122 | $0.1232 | $0.1821 | $0.4175 | 365,043 | 16,390 |  |
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

Skipped 5 `*-aborted-*` run dir(s): partial runs that were never published.

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

