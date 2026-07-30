# Comparative ladder — judged by gemini-3-flash-preview:cloud

Instead of isolated 1–5 scores, **gemini-3-flash-preview:cloud** was shown ALL models' persisted code for each question at once and asked to rank them best→worst (ties allowed). Model identities are anonymized to letters and **shuffled per question** (seeded on `question_id`) so the ranking can't lean on model names. Rank = fractional/average rank (ties share the average position); **lower is better**.

## Provenance

- generated: `2026-06-04T10:28:31.840412Z`  ·  git `5f5af00`  ·  host `solidpc`
- judge: `gemini-3-flash-preview:cloud` (num_predict=8000)  ·  languages: all
- questions laddered: 60  ·  clean parses: 57
- per-question rankings: `benchmarks/consultants/results/2026-06-04/coder_med/ladder.jsonl`

## Overall ladder

1. `kimi-k2.6:cloud`  — mean rank 2.82
2. `deepseek-v4-pro:cloud`  — mean rank 3.30
3. `minimax-m3:cloud`  — mean rank 3.71
4. `deepseek-v4-flash:cloud` = `glm-5.1:cloud`  — mean rank 4.23
6. `nemotron-3-super:cloud`  — mean rank 4.50
7. `minimax-m2.7:cloud`  — mean rank 5.17

## Per-model detail & cross-check

| model | mean rank | n_q | pass-rate | kimi q | gemini q |
|---|---:|---:|---:|---:|---:|
| `kimi-k2.6:cloud` | 2.82 | 58 | 100% | 4.19 | 4.72 |
| `deepseek-v4-pro:cloud` | 3.30 | 58 | 98% | 4.00 | 4.58 |
| `minimax-m3:cloud` | 3.71 | 58 | 97% | 3.92 | 4.55 |
| `deepseek-v4-flash:cloud` | 4.20 | 58 | 92% | 3.96 | 4.20 |
| `glm-5.1:cloud` | 4.26 | 58 | 95% | 3.85 | 4.38 |
| `nemotron-3-super:cloud` | 4.50 | 57 | 88% | 3.62 | 4.17 |
| `minimax-m2.7:cloud` | 5.17 | 58 | 77% | 3.30 | 3.73 |

_Cross-check: a sound ladder broadly tracks pass-rate and the absolute quality columns; large inversions (top of the ladder with a low pass-rate) flag a judge that rewards style over correctness._

## Per-language ladders

### c

1. `kimi-k2.6:cloud`  — mean rank 2.10
2. `deepseek-v4-pro:cloud`  — mean rank 3.25
3. `minimax-m3:cloud`  — mean rank 3.60
4. `glm-5.1:cloud`  — mean rank 4.45
5. `nemotron-3-super:cloud`  — mean rank 4.75
6. `deepseek-v4-flash:cloud` = `minimax-m2.7:cloud`  — mean rank 4.93

### cpp

1. `deepseek-v4-pro:cloud`  — mean rank 1.95
2. `deepseek-v4-flash:cloud`  — mean rank 3.45
3. `minimax-m3:cloud`  — mean rank 3.90
4. `nemotron-3-super:cloud`  — mean rank 4.10
5. `kimi-k2.6:cloud`  — mean rank 4.45
6. `minimax-m2.7:cloud`  — mean rank 5.00
7. `glm-5.1:cloud`  — mean rank 5.15

### csharp

1. `kimi-k2.6:cloud`  — mean rank 2.40
2. `minimax-m3:cloud`  — mean rank 2.75
3. `glm-5.1:cloud`  — mean rank 3.85
4. `nemotron-3-super:cloud`  — mean rank 4.06
5. `deepseek-v4-flash:cloud`  — mean rank 4.40
6. `deepseek-v4-pro:cloud` = `minimax-m2.7:cloud`  — mean rank 5.12

### go

1. `kimi-k2.6:cloud`  — mean rank 2.44
2. `deepseek-v4-pro:cloud`  — mean rank 2.89
3. `minimax-m3:cloud`  — mean rank 3.78
4. `glm-5.1:cloud`  — mean rank 4.11
5. `deepseek-v4-flash:cloud` = `nemotron-3-super:cloud`  — mean rank 4.36
7. `minimax-m2.7:cloud`  — mean rank 6.06

### python

1. `kimi-k2.6:cloud`  — mean rank 2.89
2. `deepseek-v4-flash:cloud`  — mean rank 3.06
3. `glm-5.1:cloud`  — mean rank 3.56
4. `deepseek-v4-pro:cloud`  — mean rank 3.83
5. `minimax-m3:cloud`  — mean rank 4.78
6. `minimax-m2.7:cloud`  — mean rank 4.89
7. `nemotron-3-super:cloud`  — mean rank 5.00

### rust

1. `kimi-k2.6:cloud`  — mean rank 2.60
2. `deepseek-v4-pro:cloud`  — mean rank 2.75
3. `minimax-m3:cloud`  — mean rank 3.55
4. `glm-5.1:cloud`  — mean rank 4.35
5. `deepseek-v4-flash:cloud` = `nemotron-3-super:cloud`  — mean rank 4.80
7. `minimax-m2.7:cloud`  — mean rank 5.15

---
