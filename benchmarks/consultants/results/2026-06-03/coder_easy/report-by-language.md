# Per-language scoreboard — coder_easy suite v1.0

Models: `deepseek-v4-flash:cloud`, `deepseek-v4-pro:cloud`, `glm-5.1:cloud`, `kimi-k2.6:cloud`, `minimax-m2.7:cloud`, `minimax-m3:cloud`, `nemotron-3-super:cloud`. Judge: `kimi-k2.6:cloud`. Run: `2026-06-03T19:21:35.170209Z`.

**Classification axis = normalized pass-rate.** A question that defeats *every* model carries no signal about which model is better, so it is excluded from the per-language scoring (the `#disc` column counts the questions that survive). Winners break ties on `avg_quality` then `median_tokens`. A language with **0** discriminating questions is *inconclusive* — its winner is quality-judge-only and flagged as such.

## Overall (all languages)

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`.

| Model | Pass rate | Avg quality | Median tokens | Qualifies? |
|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 99% (178/180) | 3.90 | 2416 | ✅ |
| `glm-5.1:cloud` | 99% (178/180) | 4.16 | 1944 | ✅ |
| `deepseek-v4-pro:cloud` | 98% (177/180) | 4.13 | 2418 | ✅ |
| `kimi-k2.6:cloud` | 98% (177/180) | 4.31 | 2173 | ✅ |
| `minimax-m3:cloud` | 98% (176/180) | 4.14 | 249 | ✅ |
| `minimax-m2.7:cloud` | 97% (174/180) | 3.74 | 1911 | ✅ |
| `nemotron-3-super:cloud` | 93% (168/180) | 3.92 | 2706 | ✅ |

## Per-language winners

| Language | #disc / #q | Winner | Norm pass | Full pass | Avg quality | Meets bar? |
|---|---|---|---|---|---|---|
| `c` | 30 / 30 | `kimi-k2.6:cloud` | 100% | 100% | 4.52 | ✅ |
| `cpp` | 30 / 30 | `kimi-k2.6:cloud` | 100% | 100% | 4.50 | ✅ |
| `csharp` | 30 / 30 | `kimi-k2.6:cloud` | 100% | 100% | 4.40 | ✅ |
| `go` | 30 / 30 | `deepseek-v4-flash:cloud` | 100% | 100% | 3.27 | ❌ |
| `python` | 30 / 30 | `glm-5.1:cloud` | 100% | 100% | 4.83 | ✅ |
| `rust` | 30 / 30 | `minimax-m2.7:cloud` | 100% | 100% | 3.57 | ✅ |

## Pass-rate matrix (full, all questions)

| Model | c | cpp | csharp | go | python | rust | overall |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 100% | 100% | 100% | 100% | 100% | 93% | 99% |
| `deepseek-v4-pro:cloud` | 100% | 97% | 100% | 97% | 100% | 97% | 98% |
| `glm-5.1:cloud` | 100% | 100% | 100% | 97% | 100% | 97% | 99% |
| `kimi-k2.6:cloud` | 100% | 100% | 100% | 97% | 100% | 93% | 98% |
| `minimax-m2.7:cloud` | 100% | 100% | 97% | 83% | 100% | 100% | 97% |
| `minimax-m3:cloud` | 100% | 97% | 100% | 93% | 100% | 97% | 98% |
| `nemotron-3-super:cloud` | 100% | 97% | 90% | 87% | 100% | 87% | 93% |

## Adopted routes vs. data

Compare the winners above against `consultants/engine/coder_defaults.py`. A per-language route that names a model the data does **not** crown for that language is a divergence to reconcile (or to justify on grounds the suite doesn't measure, e.g. latency/cost).
