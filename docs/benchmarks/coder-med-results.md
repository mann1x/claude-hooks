# Coder skill-eval — medium discrimination band + cross-judge (`coder_med` v1.0)

> ↟ [Benchmark index](index.md) · suite manifest:
> [`questions/coder_med/SUITE.md`](../../benchmarks/consultants/questions/coder_med/SUITE.md) ·
> easy-tier sibling: [`coder-easy-results.md`](coder-easy-results.md) ·
> hard-tier sibling: [`coder-mlang-results.md`](coder-mlang-results.md) ·
> ledger row: [`consultants-skill-eval-baselines.md`](../consultants-skill-eval-baselines.md#coder_med) ·
> adopted routes: [`consultants/engine/coder_defaults.py`](../../consultants/engine/coder_defaults.py).

`coder_med@1.0` is the **mid-band** between the saturated `coder_easy` (every
strong model ~100%) and the all-fail `coder_mlang` (10 of 13 defeat every
model). It is **10 algorithmic problems × 6 languages = 60 questions**, one
uniform stdin→stdout contract, picked to need real thinking — run-length
encoding, 3-type bracket matching, roman→int, interval merge, a precedence
expression evaluator, spiral traversal, Kadane max-subarray, top-word with
lexicographic tie-break, base conversion, and a sliding-window maximum.

This page answers two things the easy suite could not:

1. **Does a harder band separate the field on *correctness* rather than the
   judge's taste?** (Yes — pass-rate spreads 100% → 77%.)
2. **Is the `kimi-k2.6` judge biased?** A second, independent judge
   (`gemini-3-flash-preview:cloud`) re-scored every persisted solution and
   ranked all models head-to-head in a comparative **ladder**. (Yes — but
   **only on saturated easy code**, where kimi self-inflates by ~0.27 quality
   points. On the discriminating medium suite the self-bias vanishes (+0.03),
   and a neutral ladder still ranks kimi #1. See
   [Cross-judge](#cross-judge--kimi-vs-gemini).)

---

## ⚠️ Read this first — medium separates the *field*, leaders still tie per-language

The medium suite fixes the easy suite's worst property: **every one of the 60
questions discriminates** (10/10 per language carry signal — none is all-fail
*or* all-pass). Overall pass-rate now spans a real **23-point spread**
(100% → 77%), and `minimax-m2.7` drops **out of the rubric** on quality.

But the *leaders* still saturate within a language: in each language several
top models go 10/10, so the single per-language "winner" is a **quality
tie-break** — and we settle it with the **neutral gemini ladder**, not the
kimi self-judge column. Where the medium suite earns its keep is (a) the overall
reliability ranking and (b) **specific language weaknesses** the easy suite hid
— e.g. `minimax-m2.7` collapses to **60%** on csharp and go. Leader-vs-leader
is settled by the cross-judge re-score + ladder below.

**Provenance:** `coder_med` v1.0, suite hash `0e6ab0fd6b95`, judge
`kimi-k2.6:cloud`, git `5f5af00`, host solidpc, 2026-06-04. 60 questions × 7
models = 420 trials, all **100% compile**. Committed artifacts: the run's
[`report.md` + `report-by-language.md` + `metadata.json` + cross-judge
`rescore-report.md` + `ladder-report.md`](../../benchmarks/consultants/results/2026-06-04/coder_med/);
the raw `trials.jsonl` / `trials-rejudged.jsonl` / `ladder.jsonl` are
**gitignored** (regenerable payload). Cross-judge: second judge
`gemini-3-flash-preview:cloud` (`num_predict` 4000 re-score / 8000 ladder —
high budget because it is a reasoning model whose chain-of-thought spends
output tokens; an earlier 800/1000-budget pass truncated ~4–8% of replies
before the verdict line, recovered by a targeted high-budget retry).

---

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Six of seven qualify;
`minimax-m2.7` fails on the quality axis. Quality here is **kimi-judged** — see
[Cross-judge](#cross-judge--kimi-vs-gemini) for the de-biased view.

| Model | Pass rate | Avg quality (kimi) | Median comp-tok | Median wall | Qualifies? |
|---|---|---|---|---|---|
| `kimi-k2.6:cloud` | **100% (60/60)** | 4.19 | 1043 | 48.5s | ✅ |
| `deepseek-v4-pro:cloud` | 98% (59/60) | 4.00 | 476 | 53.8s | ✅ |
| `minimax-m3:cloud` | 97% (58/60) | 3.92 | 690 | 59.7s | ✅ |
| `glm-5.1:cloud` | 95% (57/60) | 3.85 | **282** | 41.0s | ✅ |
| `deepseek-v4-flash:cloud` | 92% (55/60) | 3.96 | 366 | 46.8s | ✅ |
| `nemotron-3-super:cloud` | 88% (53/60) | 3.62 | 770 | **39.0s** | ✅ |
| `minimax-m2.7:cloud` | 77% (46/60) | 3.30 | 618 | 52.3s | ❌ (quality) |

`comp-tok` = median **completion** (generated) tokens over the model's 60
trials; `wall` = median end-to-end seconds. (Prompt tokens are near-constant
across models for a shared task, so completion tokens are what differentiate —
and what the [efficiency](#efficiency--most-quality-per-token--second) ratios
divide by.)

`kimi-k2.6` is the sole 100%-pass model and tops kimi's own quality column —
but it is also the judge, so the quality ranking is self-graded (the pass
column is objective pytest and stands on its own). `glm-5.1` is again the
**efficiency leader** — the lowest completion-token median (282) at a strong
95% pass; `nemotron-3-super` is the fastest wall (39.0s) but slips on pass-rate
(88%). The full token-vs-quality / wall-vs-quality trade-off is in
[Efficiency](#efficiency--most-quality-per-token--second).

---

## Pass-rate matrix (n / 10 per language) — where the field spreads

| Model | c | cpp | csharp | go | python | rust | overall |
|---|---|---|---|---|---|---|---|
| `kimi-k2.6:cloud` | 100 | 100 | 100 | 100 | 100 | 100 | **100%** |
| `deepseek-v4-pro:cloud` | 100 | 100 | 100 | 100 | 90 | 100 | 98% |
| `minimax-m3:cloud` | 100 | 100 | 100 | 90 | 90 | 100 | 97% |
| `glm-5.1:cloud` | 100 | 90 | 100 | 100 | 100 | 80 | 95% |
| `deepseek-v4-flash:cloud` | 90 | 100 | 90 | 90 | 100 | 80 | 92% |
| `nemotron-3-super:cloud` | 100 | 90 | 90 | 80 | 80 | 90 | 88% |
| `minimax-m2.7:cloud` | 90 | 90 | **60** | **60** | 90 | 70 | 77% |

The spread the easy suite couldn't show: **csharp and go** separate the field
(60–100%), and **rust** is the broadest (70–100%). `minimax-m2.7`'s csharp/go
collapse (60%) is the clearest single-model weakness in the cohort.

---

## Per-language winners (neutral gemini ladder) + cost

Every language has 10 discriminating questions, but the *leaders* still
saturate on pass-rate within a language, so the single per-language "winner" is
settled by the **neutral gemini ladder** (head-to-head ranking, model names
anonymized — see [Comparative ladder](#comparative-ladder-gemini)), not by the
self-judged kimi quality column. The winner's median completion tokens + wall
are shown so the quality pick can be weighed against its cost.

| Lang | Winner (gemini ladder) | Ladder rank | Gemini q | Comp-tok | Wall | Runner-up (ladder) |
|---|---|---:|---:|---:|---:|---|
| **c** | `kimi-k2.6:cloud` | 2.10 | 4.80 | 1760 | 57.4s | `deepseek-v4-pro` (3.25) |
| **cpp** | `deepseek-v4-pro:cloud` | 1.95 | 4.90 | 476 | 69.3s | `deepseek-v4-flash` (3.45) |
| **csharp** | `kimi-k2.6:cloud` | 2.40 | 5.00 | 1488 | 58.1s | `minimax-m3` (2.75) |
| **go** | `kimi-k2.6:cloud` | 2.44 | 4.80 | 1017 | 47.5s | `deepseek-v4-pro` (2.89) |
| **python** | `kimi-k2.6:cloud` | 2.89 | 4.50 | 647 | 37.1s | `deepseek-v4-flash` (3.06) |
| **rust** | `kimi-k2.6:cloud` | 2.60 | 4.40 | 845 | 42.7s | `deepseek-v4-pro` (2.75) |

`kimi-k2.6` wins **5 of 6** languages on the neutral ladder (`deepseek-v4-pro`
takes cpp) — so its lead is *not* a self-judging artifact, a second judge
agrees. But kimi is also the **token-heaviest** winner (1760 comp-tok on c,
1488 on csharp, ~2–6× the leaner field) and rarely the fastest. The
**per-language quality leader and the efficiency leader are different models**:
quality → kimi; efficiency → glm-5.1 (see
[Efficiency](#efficiency--most-quality-per-token--second)).

### Full per-language scoreboard (pass · gemini q · tokens · wall · ladder)

Sorted by neutral gemini ladder rank (lower = better). `comp-tok` / `wall` are
medians over each model's 10 trials in that language.

#### c
| Model | Pass | Gemini q | Comp-tok | Wall | Ladder |
|---|---|---:|---:|---:|---:|
| `kimi-k2.6` | 10/10 | 4.80 | 1760 | 57.4s | **2.10** |
| `deepseek-v4-pro` | 10/10 | 4.80 | 616 | 61.9s | 3.25 |
| `minimax-m3` | 10/10 | 4.90 | 1368 | 63.0s | 3.60 |
| `glm-5.1` | 10/10 | 4.70 | 350 | 41.4s | 4.45 |
| `nemotron-3-super` | 10/10 | 4.40 | 1090 | 62.4s | 4.75 |
| `minimax-m2.7` | 9/10 | 4.10 | 662 | 66.9s | 4.93 |
| `deepseek-v4-flash` | 9/10 | 4.10 | 546 | 49.0s | 4.93 |

#### cpp
| Model | Pass | Gemini q | Comp-tok | Wall | Ladder |
|---|---|---:|---:|---:|---:|
| `deepseek-v4-pro` | 10/10 | 4.90 | 476 | 69.3s | **1.95** |
| `deepseek-v4-flash` | 10/10 | 4.50 | 352 | 53.0s | 3.45 |
| `minimax-m3` | 10/10 | 4.60 | 372 | 66.2s | 3.90 |
| `nemotron-3-super` | 9/10 | 4.40 | 734 | 46.5s | 4.10 |
| `kimi-k2.6` | 10/10 | 4.80 | 704 | 63.0s | 4.45 |
| `minimax-m2.7` | 9/10 | 4.20 | 558 | 54.2s | 5.00 |
| `glm-5.1` | 9/10 | 4.40 | 258 | 51.7s | 5.15 |

#### csharp
| Model | Pass | Gemini q | Comp-tok | Wall | Ladder |
|---|---|---:|---:|---:|---:|
| `kimi-k2.6` | 10/10 | 5.00 | 1488 | 58.1s | **2.40** |
| `minimax-m3` | 10/10 | 4.60 | 702 | 74.6s | 2.75 |
| `glm-5.1` | 10/10 | 4.50 | 280 | 48.1s | 3.85 |
| `nemotron-3-super` | 9/10 | 4.10 | 815 | 37.1s | 4.06 |
| `deepseek-v4-flash` | 9/10 | 4.00 | 354 | 43.6s | 4.40 |
| `minimax-m2.7` | 6/10 | 3.20 | 608 | 59.3s | 5.12 |
| `deepseek-v4-pro` | 10/10 | 4.40 | 375 | 53.7s | 5.12 |

#### go
| Model | Pass | Gemini q | Comp-tok | Wall | Ladder |
|---|---|---:|---:|---:|---:|
| `kimi-k2.6` | 10/10 | 4.80 | 1017 | 47.5s | **2.44** |
| `deepseek-v4-pro` | 10/10 | 4.50 | 476 | 57.3s | 2.89 |
| `minimax-m3` | 9/10 | 4.00 | 1058 | 61.1s | 3.78 |
| `glm-5.1` | 10/10 | 4.20 | 282 | 39.6s | 4.11 |
| `deepseek-v4-flash` | 9/10 | 4.30 | 364 | 52.5s | 4.36 |
| `nemotron-3-super` | 8/10 | 3.90 | 756 | 42.6s | 4.36 |
| `minimax-m2.7` | 6/10 | 2.70 | 832 | 55.5s | 6.06 |

#### python
| Model | Pass | Gemini q | Comp-tok | Wall | Ladder |
|---|---|---:|---:|---:|---:|
| `kimi-k2.6` | 10/10 | 4.50 | 647 | 37.1s | **2.89** |
| `deepseek-v4-flash` | 10/10 | 4.90 | 291 | 40.5s | 3.06 |
| `glm-5.1` | 10/10 | 4.80 | 210 | 36.8s | 3.56 |
| `deepseek-v4-pro` | 9/10 | 4.50 | 382 | 42.7s | 3.83 |
| `minimax-m3` | 9/10 | 4.60 | 318 | 32.5s | 4.78 |
| `minimax-m2.7` | 9/10 | 4.30 | 528 | 39.5s | 4.89 |
| `nemotron-3-super` | 8/10 | 4.20 | 578 | 29.0s | 5.00 |

#### rust
| Model | Pass | Gemini q | Comp-tok | Wall | Ladder |
|---|---|---:|---:|---:|---:|
| `kimi-k2.6` | 10/10 | 4.40 | 845 | 42.7s | **2.60** |
| `deepseek-v4-pro` | 10/10 | 4.40 | 464 | 30.4s | 2.75 |
| `minimax-m3` | 10/10 | 4.60 | 865 | 25.4s | 3.55 |
| `glm-5.1` | 8/10 | 3.70 | 361 | 24.8s | 4.35 |
| `nemotron-3-super` | 9/10 | 4.00 | 750 | 24.9s | 4.75 |
| `deepseek-v4-flash` | 8/10 | 3.40 | 454 | 28.9s | 4.85 |
| `minimax-m2.7` | 7/10 | 3.90 | 680 | 43.4s | 5.15 |

---

## Cross-judge — kimi vs gemini

`kimi-k2.6` was the **sole** judge for both the easy and medium runs **and** a
competitor in the cohort. To test for self-judge bias,
`gemini-3-flash-preview:cloud` (a reasoning model, run at `num_predict=4000`)
re-scored every persisted solution against the **same** 1–5 rubric. The
original `quality_score` is untouched; gemini's lands in
`quality_secondary_score`.

### Easy suite (saturated 4 languages, 840 trials)

> Re-score artifact:
> [`easy-rescore/rescore-report.md`](../../benchmarks/consultants/results/2026-06-04/easy-rescore/rescore-report.md).
> Coverage: 839 numeric / 0 unparseable / 1 no-source (full coverage after the
> high-budget retry; the initial 800-token pass had left 37 reasoning replies
> truncated before the `SCORE:` line).

**The self-judge bias is real and quantified.** gemini scores **every other
model's** code *higher* than kimi does (per-model Δ +0.17 … +0.40). Kimi's own
code is the **sole exception** — gemini scores it *lower* (−0.03):

| | kimi self-score | gemini on same code | Δ |
|---|---:|---:|---:|
| kimi's own rows (120) | 4.54 | 4.51 | **−0.03** |
| cohort-wide (839) | 4.29 | 4.52 | **+0.24** |

The **gap (self − cohort) = −0.27** is the bias: kimi rates its own code ~0.27
points higher, relative to a neutral judge, than it rates the field. Where the
easy suite's c/csharp "winner" was kimi on a single-judge quality margin of
0.13–0.14, that margin is **inside** the measured self-inflation — i.e. not
trustworthy. By language the bias rides on cpp (+0.33 cohort Δ) and c/csharp
(+0.27); python is nearly judge-agnostic (+0.08).

### Medium suite (all 6 languages, 420 trials)

> Re-score artifact:
> [`coder_med/rescore-report.md`](../../benchmarks/consultants/results/2026-06-04/coder_med/rescore-report.md).
> Coverage: **420 numeric / 0 unparseable / 0 no-source** (full coverage; the
> high-budget retry recovered the 34 replies the initial pass had truncated).

**The bias does *not* carry to the medium suite — this is the key nuance.** On
substantive code, kimi judges its own work the same as a neutral judge does:

| | kimi self-score | gemini on same code | Δ |
|---|---:|---:|---:|
| kimi's own rows (60) | 4.19 | 4.72 | +0.53 |
| cohort-wide (420) | 3.83 | 4.33 | +0.50 |

The **gap (self − cohort) = +0.03** — no self-favoring. So kimi's self-judge
bias is **confined to trivial / saturated code** (easy), where stylistic taste
dominates; on the discriminating medium problems, where correctness and
substance dominate, it vanishes.

gemini is systematically the more generous judge (cohort Δ **+0.50**, larger
than easy's +0.24), and the gap is widest on **go (+0.69)** and **rust
(+0.60)** — kimi rates go/rust at 3.37/3.46 where gemini sees 4.06/4.06,
suggesting kimi **under-rates go/rust idioms**. python is the most
judge-agnostic (+0.24).

| language | n | kimi | gemini | Δ |
|---|---:|---:|---:|---:|
| c | 70 | 4.08 | 4.54 | +0.47 |
| cpp | 70 | 4.16 | 4.54 | +0.38 |
| csharp | 70 | 3.70 | 4.26 | +0.56 |
| go | 70 | 3.37 | 4.06 | **+0.69** |
| python | 70 | 4.30 | 4.54 | +0.24 |
| rust | 70 | 3.46 | 4.06 | +0.60 |

---

## Comparative ladder (gemini)

Beyond absolute 1–5 scores, `gemini-3-flash-preview:cloud` was shown **all
models' code for one question at once** and asked to rank them best→worst with
ties. Model identities are anonymized to letters and **shuffled per question**
(seeded on `question_id`) so the ranking can't lean on model names. Rank =
fractional/average rank (ties share the average position); **lower is better**.

> Ladder artifacts:
> [`coder_med/ladder-report.md`](../../benchmarks/consultants/results/2026-06-04/coder_med/ladder-report.md)
> (60 questions, **57 clean parses**) ·
> [`easy-rescore/ladder-report.md`](../../benchmarks/consultants/results/2026-06-04/easy-rescore/ladder-report.md)
> (120 saturated questions, **117 clean parses**). After raising the ladder
> budget to `num_predict=8000`, the parse rate is **95–98%**; the few drops are
> gemini still running out of budget on the long 7-solution prompts and omitting
> a clean `RANKING:` line, and are excluded from the means.

### Medium suite ladder — tracks pass-rate, confirms kimi's win is real

The neutral comparative ladder ranks the field essentially in **pass-rate
order**, with no inversions — a sound ladder. Critically, even though gemini
(not kimi) is doing the ranking here, **kimi still places #1** — so kimi's
medium lead is *not* a self-judging artifact; a neutral judge agrees:

| Rung | Model | Mean rank | Pass-rate |
|---|---|---:|---:|
| 1 | `kimi-k2.6:cloud` | 2.82 | 100% |
| 2 | `deepseek-v4-pro:cloud` | 3.30 | 98% |
| 3 | `minimax-m3:cloud` | 3.71 | 97% |
| 4 | `deepseek-v4-flash:cloud` = `glm-5.1:cloud` | 4.23 | 92% / 95% |
| 6 | `nemotron-3-super:cloud` | 4.50 | 88% |
| 7 | `minimax-m2.7:cloud` | 5.17 | 77% |

Per-language, the ladder splits the leaders: **`deepseek-v4-pro` wins cpp
(1.95)**, while **`kimi` wins the other five** (c 2.10, csharp 2.40, go 2.44,
python 2.89, rust 2.60) — a cleaner leader-vs-leader signal than the
single-judge quality tie-breaks above. (rust is the closest call: kimi 2.60 vs
`deepseek-v4-pro` 2.75.)

### Easy suite ladder — compresses to a near-tie (corroborates the bias)

On the saturated easy code the ladder spread collapses (mean ranks 3.36–4.58
vs medium's 2.82–5.17) — near-equivalent code can't be cleanly ordered. The
tell: **kimi's easy lead nearly evaporates under the neutral judge**
(kimi 3.36 ≈ `minimax-m3` 3.47, a 0.11 gap where the medium ladder separated
kimi by 0.48), exactly where kimi's *absolute self-score* had inflated it.
Remove the self-judging and kimi and minimax-m3 are effectively tied:

| Rung | Model | Mean rank |
|---|---|---:|
| 1 | `kimi-k2.6:cloud` | 3.36 |
| 2 | `minimax-m3:cloud` | 3.47 |
| 3 | `deepseek-v4-pro:cloud` | 3.87 |
| 4 | `glm-5.1:cloud` = `nemotron-3-super:cloud` | 4.12 |
| 6 | `deepseek-v4-flash:cloud` | 4.44 |
| 7 | `minimax-m2.7:cloud` | 4.58 |

The easy per-language ladders **agree with the non-self-judged kimi quality
picks** (c→`minimax-m3` 2.79, python→`glm-5.1` 3.43) and still rank kimi top of
cpp (2.88) and csharp (2.76); the bias shows not in those per-language tops but
in the **overall** compression — where the medium ladder cleanly separates kimi
at #1, the easy ladder cannot.

---

## Efficiency — most quality per token / second

Quality here is the **neutral gemini judge** (kimi self-inflates on its own
code, so its column can't arbitrate efficiency). Two ratios, both over the
medians from [Per-model summary](#per-model-summary):

- **Token-efficiency** = gemini-q ÷ (median comp-tok / 1000) — quality points
  per 1k **generated** tokens.
- **Speed-efficiency** = gemini-q ÷ (median wall / 10) — quality points per 10 s.

**Quality gate.** Cheap-but-wrong is not "efficient", so only models clearing
**pass ≥ 90% AND gemini-q ≥ 4.0** are eligible to win. That admits five —
`kimi`, `deepseek-v4-pro`, `minimax-m3`, `glm-5.1`, `deepseek-v4-flash`;
`nemotron-3-super` (88% pass) and `minimax-m2.7` (77%) are gated out (struck
rows below).

| Model | Pass | Gemini q | Comp-tok | Wall | Q / 1k-tok | Q / 10s | Balanced† | Gate |
|---|---|---:|---:|---:|---:|---:|---:|:--:|
| `glm-5.1:cloud` | 95% | 4.38 | 282 | 41.0s | **15.57** | **1.07** | **4.08** | ✅ |
| `deepseek-v4-flash:cloud` | 92% | 4.20 | 366 | 46.8s | 11.48 | 0.90 | 3.21 | ✅ |
| `deepseek-v4-pro:cloud` | 98% | 4.58 | 476 | 53.8s | 9.62 | 0.85 | 2.86 | ✅ |
| `minimax-m3:cloud` | 97% | 4.55 | 690 | 59.7s | 6.59 | 0.76 | 2.24 | ✅ |
| `kimi-k2.6:cloud` | 100% | 4.72 | 1043 | 48.5s | 4.52 | 0.97 | 2.09 | ✅ |
| ~~`nemotron-3-super:cloud`~~ | 88% | 4.17 | 770 | 39.0s | 5.41 | 1.07 | 2.41 | ✖ pass<90 |
| ~~`minimax-m2.7:cloud`~~ | 77% | 3.73 | 618 | 52.3s | 6.04 | 0.71 | 2.07 | ✖ pass<90 |

† Balanced = geometric mean of the two efficiency ratios (rewards a model that
leads *both* axes, not one at the other's expense).

**Winners (within the quality gate):**

- **Token-efficiency → `glm-5.1` (15.57)** — 1.4× the runner-up
  `deepseek-v4-flash` (11.48) and 3.4× the quality leader `kimi` (4.52). glm
  gets near-top quality out of the fewest generated tokens.
- **Speed-efficiency → `glm-5.1` (1.07 Q/10s)** — fastest *eligible* model per
  quality point (the gated-out `nemotron` ties it on the raw ratio but fails the
  pass gate).
- **Balanced ("most efficient while keeping quality") → `glm-5.1` (4.08)** — it
  leads *both* axes simultaneously, clear of `deepseek-v4-flash` (3.21).

The headline trade-off: **`kimi` is the per-language quality/correctness leader
(wins 5/6 on the neutral ladder) but the *least* token-efficient of the gated
set** (1043 comp-tok, 3.7× glm). **`glm-5.1` trades ~0.34 gemini-quality points
for ~3.7× fewer generated tokens and the fastest eligible wall (41.0s)** — which
is exactly why [`coder_defaults.py`](../../consultants/engine/coder_defaults.py)
already routes the default coder to `glm-5.1:cloud`. This run **corroborates**
that route; it does not change it.

---

## What this suite establishes

1. **A real mid-band exists.** `coder_med` separates the field on correctness
   (100% → 77%) where easy saturated — and surfaces specific language
   weaknesses (minimax-m2.7 csharp/go 60%) the easy suite hid.
2. **The kimi self-judge bias is real but *suite-dependent*.** It shows on
   saturated easy code (−0.27 self-vs-cohort gap; the neutral ladder collapses
   kimi's easy lead to a tie) but **not** on the medium suite (+0.03 gap;
   neutral ladder still ranks kimi #1 tracking pass-rate). Treatment: trust the
   single-judge quality column on *discriminating* suites; for *saturated*
   ones, defer to the gemini re-score + comparative ladder. gemini also
   systematically under-disagrees with kimi on go/rust (kimi under-rates those
   idioms by ~0.6 pts).
3. **Quality leader ≠ efficiency leader.** On the neutral ladder `kimi` wins
   5/6 languages on correctness/quality, but it is the most token-heavy of the
   quality-gated models (1043 comp-tok). `glm-5.1` is the **balanced efficiency
   winner** — top of both token- and speed-efficiency within the pass ≥ 90% /
   q ≥ 4.0 gate, trading ~0.34 quality points for ~3.7× fewer generated tokens.
   See [Efficiency](#efficiency--most-quality-per-token--second).
4. **Routes are NOT changed by this page** — winners are mirrored + annotated
   against [`coder_defaults.py`](../../consultants/engine/coder_defaults.py).
   The efficiency result **corroborates** the existing `glm-5.1` default coder
   route. Any realignment is a separate decision; see [Follow-ups](#follow-ups).

---

## Follow-ups

- **Reconcile per-language routes** against the medium winners + the gemini
  ladder once both judges agree.
- **Retire kimi as the sole judge.** Adopt a cross-judge panel
  (`--audit-judge-model` / `--meta-judge-model`, or the offline gemini
  re-score) for any quality-decided pick, given the measured self-bias.
- **Gemini judge coverage — solved for this run.** The reasoning model truncates
  before the verdict line at a low `num_predict`; raising it to 4000 (re-score)
  / 8000 (ladder) and a targeted retry of only the truncated items took the
  re-score to **100%** (420/420 med, 839/840 easy) and the ladder to **95–98%**
  (57/60 med, 117/120 easy). Bake the higher budget into any future gemini-judge
  run (`rejudge.py` defaults are conservative; pass `--num-predict`).

---

## Reproduce

```bash
# Per-model + per-language scoreboards from the merged run
python benchmarks/consultants/analyze.py \
    benchmarks/consultants/results/2026-06-04/coder_med/trials.jsonl
python benchmarks/consultants/analyze.py \
    benchmarks/consultants/results/2026-06-04/coder_med/trials.jsonl --by-language

# Re-render the cross-judge report from the saved rejudged jsonl (no cloud)
python benchmarks/consultants/rejudge.py --rescore --report-only \
    --trials benchmarks/consultants/results/2026-06-04/coder_med/trials-rejudged.jsonl \
    --report /tmp/med-rescore.md
```

Full live re-run (7-model cohort + gemini second judge): see the run's
`_launch.sh` / `_merge.sh` / `_downstream.sh` next to the results. If
`suite_hash` differs from `0e6ab0fd6b95`, the question content drifted without
a SUITE.md version bump — investigate before comparing.

---

## Related

- [Benchmark index](index.md) — all benchmark families.
- [Coder easy results](coder-easy-results.md) — the saturated floor this band sits above.
- [Coder per-language (hard tier)](coder-mlang-results.md) — the all-fail ceiling.
- [Skill-eval protocol](../consultants-skill-eval-protocol.md) — methodology + rubric.
