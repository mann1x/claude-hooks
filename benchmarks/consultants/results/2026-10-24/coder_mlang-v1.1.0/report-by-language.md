# Per-language scoreboard — coder_mlang suite v1.1.0

Models: `deepseek-v4-pro:cloud`, `deepseek-v4.1-flash:cloud`, `glm-5.3-flash:cloud`, `glm-5.3:cloud`, `minimax-m3:cloud`. Judge: `(none)`. Run: `2026-10-24T07:42:06.618532Z`.

**Classification axis = normalized pass-rate.** A question that defeats *every* model carries no signal about which model is better, so it is excluded from the per-language scoring (the `#disc` column counts the questions that survive). Winners break ties on `avg_quality` then `median_tokens`. A language with **0** discriminating questions is *inconclusive* — its winner is quality-judge-only and flagged as such.

## Overall (all languages)

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`.

| Model | Pass rate | Avg quality | Median tokens | Qualifies? |
|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | 100% (18/18) | 4.78 | 8902 | ✅ |
| `deepseek-v4.1-flash:cloud` | 89% (16/18) | 4.94 | 6298 | ✅ |
| `minimax-m3:cloud` | 89% (16/18) | 4.61 | 9146 | ✅ |
| `glm-5.3:cloud` | 83% (15/18) | 4.33 | 5590 | ✅ |
| `glm-5.3-flash:cloud` | 72% (13/18) | 4.00 | 5358 | ✅ |

## Per-language winners

| Language | #disc / #q | Winner | Norm pass | Full pass | Avg quality | Meets bar? |
|---|---|---|---|---|---|---|
| `c` | 3 / 3 | `minimax-m3:cloud` | 100% | 100% | 5.00 | ✅ |
| `cpp` | 3 / 3 | `deepseek-v4.1-flash:cloud` | 100% | 100% | 4.67 | ✅ |
| `csharp` | 3 / 3 | `deepseek-v4.1-flash:cloud` | 100% | 100% | 5.00 | ✅ |
| `go` | 3 / 3 | `glm-5.3:cloud` | 100% | 100% | 5.00 | ✅ |
| `python` | 3 / 3 | `glm-5.3-flash:cloud` | 100% | 100% | 5.00 | ✅ |
| `rust` | 3 / 3 | `deepseek-v4-pro:cloud` | 100% | 100% | 5.00 | ✅ |

## Pass-rate matrix (full, all questions)

| Model | c | cpp | csharp | go | python | rust | overall |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-pro:cloud` | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `deepseek-v4.1-flash:cloud` | 67% | 100% | 100% | 100% | 100% | 67% | 89% |
| `glm-5.3-flash:cloud` | 67% | 67% | 67% | 67% | 100% | 67% | 72% |
| `glm-5.3:cloud` | 67% | 100% | 100% | 100% | 100% | 33% | 83% |
| `minimax-m3:cloud` | 100% | 67% | 100% | 100% | 100% | 67% | 89% |

## Adopted routes vs. data

Compare the winners above against `consultants/engine/coder_defaults.py`. A per-language route that names a model the data does **not** crown for that language is a divergence to reconcile (or to justify on grounds the suite doesn't measure, e.g. latency/cost).
