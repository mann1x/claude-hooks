# Cross-judge re-score — kimi-k2.6:cloud vs gemini-3-flash-preview:cloud

Second, independent judge **gemini-3-flash-preview:cloud** re-scored the persisted code from a prior `coder_bench` run, using the same absolute 1–5 rubric the primary judge (**kimi-k2.6:cloud**) used. Additive: the original `quality_score` is untouched; gemini's score lands in `quality_secondary_score`.

## Provenance

- generated: `2026-06-04T10:25:15.857001Z`  ·  git `5f5af00`  ·  host `solidpc`
- secondary judge: `gemini-3-flash-preview:cloud`  (num_predict=4000)
- language filter: c, cpp, csharp, python
- input trials: 1260  ·  judged: 840  ·  **numeric score: 839**  ·  unparseable judge reply (no SCORE line): 0  ·  no source / silent: 1
- rejudged jsonl: `benchmarks/consultants/results/2026-06-04/easy-rescore/trials-rejudged.jsonl`

## Headline

- **kimi-k2.6:cloud** mean quality: **4.29**
- **gemini-3-flash-preview:cloud** mean quality: **4.52**
- cohort-wide delta (gemini − kimi): **+0.24**

## Per-language (mean quality)

| language | n | kimi-k2.6:cloud | gemini-3-flash-preview:cloud | Δ (gem−kimi) |
|---|---:|---:|---:|---:|
| c | 210 | 4.26 | 4.52 | +0.27 |
| cpp | 209 | 4.25 | 4.58 | +0.33 |
| csharp | 210 | 4.01 | 4.28 | +0.27 |
| python | 210 | 4.62 | 4.70 | +0.08 |

## Per-model (mean quality)

| model | n | kimi-k2.6:cloud | gemini-3-flash-preview:cloud | Δ (gem−kimi) |
|---|---:|---:|---:|---:|
| `minimax-m3:cloud` | 120 | 4.30 | 4.67 | +0.38 |
| `deepseek-v4-pro:cloud` | 120 | 4.44 | 4.66 | +0.22 |
| `glm-5.1:cloud` | 120 | 4.42 | 4.63 | +0.21 |
| `kimi-k2.6:cloud` | 120 | 4.54 | 4.51 | -0.03 |
| `nemotron-3-super:cloud` | 119 | 4.10 | 4.50 | +0.40 |
| `deepseek-v4-flash:cloud` | 120 | 4.16 | 4.47 | +0.31 |
| `minimax-m2.7:cloud` | 120 | 4.03 | 4.20 | +0.17 |

## Self-judge bias check

`kimi-k2.6:cloud` was the sole primary judge **and** a competitor. On its own 120 rows (coder family = `kimi`):

- kimi-k2.6:cloud self-score: **4.54**  ·  gemini-3-flash-preview:cloud on the same code: **4.51**
- self delta (gem−kimi): **-0.03**  ·  cohort delta: **+0.24**
- gap (self − cohort): **-0.27** → primary judge **inflates its own code** vs the second judge more than the cohort average — evidence of self-judge bias

---

_Numbers recomputable from the rejudged jsonl: mean of `quality_score` (primary) and `quality_secondary_score` (gemini) grouped by model / language._
