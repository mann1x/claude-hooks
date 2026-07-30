# Comparative ladder — judged by gemini-3-flash-preview:cloud

Instead of isolated 1–5 scores, **gemini-3-flash-preview:cloud** was shown ALL models' persisted code for each question at once and asked to rank them best→worst (ties allowed). Model identities are anonymized to letters and **shuffled per question** (seeded on `question_id`) so the ranking can't lean on model names. Rank = fractional/average rank (ties share the average position); **lower is better**.

## Provenance

- generated: `2026-06-04T10:32:07.794177Z`  ·  git `5f5af00`  ·  host `solidpc`
- judge: `gemini-3-flash-preview:cloud` (num_predict=8000)  ·  languages: c, cpp, csharp, python
- questions laddered: 120  ·  clean parses: 117
- per-question rankings: `benchmarks/consultants/results/2026-06-04/easy-rescore/ladder.jsonl`

## Overall ladder

1. `kimi-k2.6:cloud`  — mean rank 3.36
2. `minimax-m3:cloud`  — mean rank 3.47
3. `deepseek-v4-pro:cloud`  — mean rank 3.87
4. `glm-5.1:cloud` = `nemotron-3-super:cloud`  — mean rank 4.12
6. `deepseek-v4-flash:cloud`  — mean rank 4.44
7. `minimax-m2.7:cloud`  — mean rank 4.58

## Per-model detail & cross-check

| model | mean rank | n_q | pass-rate | kimi q | gemini q |
|---|---:|---:|---:|---:|---:|
| `kimi-k2.6:cloud` | 3.36 | 117 | 100% | 4.54 | 4.51 |
| `minimax-m3:cloud` | 3.47 | 117 | 99% | 4.30 | 4.67 |
| `deepseek-v4-pro:cloud` | 3.87 | 117 | 99% | 4.44 | 4.66 |
| `glm-5.1:cloud` | 4.10 | 117 | 100% | 4.42 | 4.63 |
| `nemotron-3-super:cloud` | 4.15 | 116 | 97% | 4.10 | 4.50 |
| `deepseek-v4-flash:cloud` | 4.44 | 117 | 100% | 4.16 | 4.47 |
| `minimax-m2.7:cloud` | 4.58 | 117 | 99% | 4.03 | 4.20 |

_Cross-check: a sound ladder broadly tracks pass-rate and the absolute quality columns; large inversions (top of the ladder with a low pass-rate) flag a judge that rewards style over correctness._

## Per-language ladders

### c

1. `minimax-m3:cloud`  — mean rank 2.79
2. `kimi-k2.6:cloud`  — mean rank 3.10
3. `nemotron-3-super:cloud`  — mean rank 3.62
4. `minimax-m2.7:cloud`  — mean rank 4.28
5. `deepseek-v4-pro:cloud`  — mean rank 4.41
6. `glm-5.1:cloud`  — mean rank 4.62
7. `deepseek-v4-flash:cloud`  — mean rank 5.17

### cpp

1. `kimi-k2.6:cloud`  — mean rank 2.88
2. `deepseek-v4-pro:cloud`  — mean rank 3.67
3. `deepseek-v4-flash:cloud` = `minimax-m3:cloud`  — mean rank 3.88
5. `nemotron-3-super:cloud`  — mean rank 4.05
6. `glm-5.1:cloud`  — mean rank 4.67
7. `minimax-m2.7:cloud`  — mean rank 4.87

### csharp

1. `kimi-k2.6:cloud`  — mean rank 2.76
2. `minimax-m3:cloud`  — mean rank 3.45
3. `glm-5.1:cloud`  — mean rank 3.66
4. `deepseek-v4-pro:cloud`  — mean rank 3.78
5. `nemotron-3-super:cloud`  — mean rank 4.38
6. `deepseek-v4-flash:cloud`  — mean rank 4.66
7. `minimax-m2.7:cloud`  — mean rank 5.33

### python

1. `glm-5.1:cloud`  — mean rank 3.43
2. `deepseek-v4-pro:cloud`  — mean rank 3.64
3. `minimax-m2.7:cloud` = `minimax-m3:cloud`  — mean rank 3.82
5. `deepseek-v4-flash:cloud`  — mean rank 4.03
6. `nemotron-3-super:cloud`  — mean rank 4.55
7. `kimi-k2.6:cloud`  — mean rank 4.71

---
