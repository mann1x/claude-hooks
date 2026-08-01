# Cross-judge re-score — kimi-k2.6:cloud vs gemini-3-flash-preview:cloud

Second, independent judge **gemini-3-flash-preview:cloud** re-scored the persisted code from a prior `coder_bench` run, using the same absolute 1–5 rubric the primary judge (**kimi-k2.6:cloud**) used. Additive: the original `quality_score` is untouched; gemini's score lands in `quality_secondary_score`.

## Provenance

- generated: `2026-06-04T06:51:45.361897Z`  ·  git `5f5af00`  ·  host `solidpc`
- secondary judge: `gemini-3-flash-preview:cloud`  (num_predict=800)
- language filter: c, cpp, csharp, python
- input trials: 1260  ·  rescored: 5  ·  skipped (no source / judge silent): 0
- rejudged jsonl: `benchmarks/consultants/results/2026-06-04/gemini-probe/trials-rejudged.jsonl`

## Headline

- **kimi-k2.6:cloud** mean quality: **4.80**
- **gemini-3-flash-preview:cloud** mean quality: **4.80**
- cohort-wide delta (gemini − kimi): **+0.00**

## Per-language (mean quality)

| language | n | kimi-k2.6:cloud | gemini-3-flash-preview:cloud | Δ (gem−kimi) |
|---|---:|---:|---:|---:|
| c | 5 | 4.80 | 4.80 | +0.00 |

## Per-model (mean quality)

| model | n | kimi-k2.6:cloud | gemini-3-flash-preview:cloud | Δ (gem−kimi) |
|---|---:|---:|---:|---:|
| `glm-5.1:cloud` | 5 | 4.80 | 4.80 | +0.00 |

## Self-judge bias check

No rows where the coder family matches the primary judge (`kimi`); self-judge bias not directly measurable on this cohort.

---

_Numbers recomputable from the rejudged jsonl: mean of `quality_score` (primary) and `quality_secondary_score` (gemini) grouped by model / language._
