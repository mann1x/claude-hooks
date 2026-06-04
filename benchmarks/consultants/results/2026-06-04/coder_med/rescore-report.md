# Cross-judge re-score — kimi-k2.6:cloud vs gemini-3-flash-preview:cloud

Second, independent judge **gemini-3-flash-preview:cloud** re-scored the persisted code from a prior `coder_bench` run, using the same absolute 1–5 rubric the primary judge (**kimi-k2.6:cloud**) used. Additive: the original `quality_score` is untouched; gemini's score lands in `quality_secondary_score`.

## Provenance

- generated: `2026-06-04T10:23:33.300943Z`  ·  git `5f5af00`  ·  host `solidpc`
- secondary judge: `gemini-3-flash-preview:cloud`  (num_predict=4000)
- language filter: all
- input trials: 420  ·  judged: 420  ·  **numeric score: 420**  ·  unparseable judge reply (no SCORE line): 0  ·  no source / silent: 0
- rejudged jsonl: `benchmarks/consultants/results/2026-06-04/coder_med/trials-rejudged.jsonl`

## Headline

- **kimi-k2.6:cloud** mean quality: **3.83**
- **gemini-3-flash-preview:cloud** mean quality: **4.33**
- cohort-wide delta (gemini − kimi): **+0.50**

## Per-language (mean quality)

| language | n | kimi-k2.6:cloud | gemini-3-flash-preview:cloud | Δ (gem−kimi) |
|---|---:|---:|---:|---:|
| c | 70 | 4.08 | 4.54 | +0.47 |
| cpp | 70 | 4.16 | 4.54 | +0.38 |
| csharp | 70 | 3.70 | 4.26 | +0.56 |
| go | 70 | 3.37 | 4.06 | +0.69 |
| python | 70 | 4.30 | 4.54 | +0.24 |
| rust | 70 | 3.46 | 4.06 | +0.60 |

## Per-model (mean quality)

| model | n | kimi-k2.6:cloud | gemini-3-flash-preview:cloud | Δ (gem−kimi) |
|---|---:|---:|---:|---:|
| `kimi-k2.6:cloud` | 60 | 4.19 | 4.72 | +0.53 |
| `deepseek-v4-pro:cloud` | 60 | 4.00 | 4.58 | +0.58 |
| `minimax-m3:cloud` | 60 | 3.92 | 4.55 | +0.63 |
| `glm-5.1:cloud` | 60 | 3.85 | 4.38 | +0.53 |
| `deepseek-v4-flash:cloud` | 60 | 3.96 | 4.20 | +0.24 |
| `nemotron-3-super:cloud` | 60 | 3.62 | 4.17 | +0.55 |
| `minimax-m2.7:cloud` | 60 | 3.30 | 3.73 | +0.43 |

## Self-judge bias check

`kimi-k2.6:cloud` was the sole primary judge **and** a competitor. On its own 60 rows (coder family = `kimi`):

- kimi-k2.6:cloud self-score: **4.19**  ·  gemini-3-flash-preview:cloud on the same code: **4.72**
- self delta (gem−kimi): **+0.53**  ·  cohort delta: **+0.50**
- gap (self − cohort): **+0.03** → no material self-favoring beyond the cohort-wide judge gap

---

_Numbers recomputable from the rejudged jsonl: mean of `quality_score` (primary) and `quality_secondary_score` (gemini) grouped by model / language._
