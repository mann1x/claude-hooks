# Per-language scoreboard — coder_med suite v1.0

Models: `deepseek-v4-flash:cloud`, `deepseek-v4-pro:cloud`, `glm-5.1:cloud`, `kimi-k2.6:cloud`, `minimax-m2.7:cloud`, `minimax-m3:cloud`, `nemotron-3-super:cloud`. Judge: `kimi-k2.6:cloud`. Run: `2026-06-04T06:40:57.968379Z`.

**Classification axis = normalized pass-rate.** A question that defeats *every* model carries no signal about which model is better, so it is excluded from the per-language scoring (the `#disc` column counts the questions that survive). Winners break ties on `avg_quality` then `median_tokens`. A language with **0** discriminating questions is *inconclusive* — its winner is quality-judge-only and flagged as such.

## Overall (all languages)

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`.

| Model | Pass rate | Avg quality | Median tokens | Qualifies? |
|---|---|---|---|---|
| `kimi-k2.6:cloud` | 100% (60/60) | 4.19 | 3069 | ✅ |
| `deepseek-v4-pro:cloud` | 98% (59/60) | 4.00 | 2964 | ✅ |
| `minimax-m3:cloud` | 97% (58/60) | 3.92 | 3581 | ✅ |
| `glm-5.1:cloud` | 95% (57/60) | 3.85 | 2529 | ✅ |
| `deepseek-v4-flash:cloud` | 92% (55/60) | 3.96 | 2896 | ✅ |
| `nemotron-3-super:cloud` | 88% (53/60) | 3.62 | 3413 | ✅ |
| `minimax-m2.7:cloud` | 77% (46/60) | 3.30 | 2766 | ❌ |

## Per-language winners

| Language | #disc / #q | Winner | Norm pass | Full pass | Avg quality | Meets bar? |
|---|---|---|---|---|---|---|
| `c` | 10 / 10 | `minimax-m3:cloud` | 100% | 100% | 4.80 | ✅ |
| `cpp` | 10 / 10 | `deepseek-v4-flash:cloud` | 100% | 100% | 4.60 | ✅ |
| `csharp` | 10 / 10 | `glm-5.1:cloud` | 100% | 100% | 4.29 | ✅ |
| `go` | 10 / 10 | `kimi-k2.6:cloud` | 100% | 100% | 3.78 | ✅ |
| `python` | 10 / 10 | `glm-5.1:cloud` | 100% | 100% | 4.60 | ✅ |
| `rust` | 10 / 10 | `minimax-m3:cloud` | 100% | 100% | 3.90 | ✅ |

## Pass-rate matrix (full, all questions)

| Model | c | cpp | csharp | go | python | rust | overall |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 90% | 100% | 90% | 90% | 100% | 80% | 92% |
| `deepseek-v4-pro:cloud` | 100% | 100% | 100% | 100% | 90% | 100% | 98% |
| `glm-5.1:cloud` | 100% | 90% | 100% | 100% | 100% | 80% | 95% |
| `kimi-k2.6:cloud` | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `minimax-m2.7:cloud` | 90% | 90% | 60% | 60% | 90% | 70% | 77% |
| `minimax-m3:cloud` | 100% | 100% | 100% | 90% | 90% | 100% | 97% |
| `nemotron-3-super:cloud` | 100% | 90% | 90% | 80% | 80% | 90% | 88% |

## Adopted routes vs. data

Compare the winners above against `consultants/engine/coder_defaults.py`. A per-language route that names a model the data does **not** crown for that language is a divergence to reconcile (or to justify on grounds the suite doesn't measure, e.g. latency/cost).
