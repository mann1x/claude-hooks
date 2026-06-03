# Coder skill-eval — easy multi-language results (`coder_easy` v1.0)

> ↟ [Benchmark index](index.md) · suite manifest:
> [`questions/coder_easy/SUITE.md`](../../benchmarks/consultants/questions/coder_easy/SUITE.md) ·
> ledger row: [`consultants-skill-eval-baselines.md`](../consultants-skill-eval-baselines.md#v10-easy-baseline-2026-06-03--per-language-reliability-floor) ·
> hard-tier sibling: [`coder-mlang-results.md`](coder-mlang-results.md) ·
> adopted routes in code:
> [`consultants/engine/coder_defaults.py`](../../consultants/engine/coder_defaults.py).

`coder_easy@1.0` is the **floor-fixing** counterpart to `coder_mlang@1.0.1`.
Where mlang was *too hard* (10 of 13 questions defeat every model, so most
per-language picks were quality-judge-only), `coder_easy` is **30 deliberately
easy problems × 6 languages = 180 questions**, one uniform contract — read
stdin, write stdout — so every model can attempt every question and the
discrimination floor disappears.

This page answers **"on basic, everyday coding, which models are reliable in
each language, and how do the two new candidates (`minimax-m3`,
`nemotron-3-super`) stack up?"**

---

## ⚠️ Read this first — the floor is fixed, but the top *saturates*

`coder_easy` cures the mlang problem completely: **all 180 questions
discriminate — zero are all-fail.** Full and normalized scoring are therefore
identical here (nothing is excluded). The trade is the opposite failure mode:
on the *pass-rate* axis the strong models **saturate near 100%** for four of
the six languages, so for those the per-language winner is decided by the
`avg_quality` → `median_tokens` tie-break, not by correctness.

> **What separates models here, by language:**
> - **go, rust** — genuine *pass-rate* separation. A clear test-based winner.
> - **c, cpp, csharp, python** — pass-rate saturated (most/all models 100%).
>   The pick is a **quality/cost** judgement, not a correctness one.

**Single-judge caveat.** This run used **one judge, `kimi-k2.6:cloud`**, which
is also a model under test (no `--audit-judge-model`). Where a language is
decided purely on quality (c/cpp/csharp/python), and `kimi` is the nominal
winner, treat it as a **self-judged** result — `kimi` ranks #1 on judge-quality
in c/csharp/go but only #2 (cpp) and #3 (python, rust) elsewhere, so it is not
blatantly self-inflating, but the c/csharp margins are 0.13–0.14 quality points
from a competitor-judge. A cross-judge re-run (`--audit-judge-model glm-5.1` +
`--meta-judge-model gemma4:31b-cloud`) is the way to de-bias the saturated
languages; see [Follow-ups](#follow-ups).

**Provenance:** `coder_easy` v1.0, suite hash `76440a746bdd`, judge
`kimi-k2.6:cloud`, git `d0fc724`, host solidpc, 2026-06-03→04. 180 questions ×
7 models = 1260 trials. Committed artifacts: the run's
[`report.md` + `report-by-language.md` + `metadata.json`](../../benchmarks/consultants/results/2026-06-03/coder_easy/);
the raw `trials.jsonl` (2.5 MB, 1260 rows) is **gitignored** per the
[results convention](../../benchmarks/consultants/README.md#where-the-results-live)
(regenerable payload). The numbers below are recomputed from that local
`trials.jsonl` — see [Reproduce](#reproduce).

---

## Per-model summary

Rubric: `pass_rate ≥ 70%` AND `avg_quality ≥ 3.5`. **All seven qualify** — the
easy suite is a competence *floor*, and every candidate clears it. `passes_tests`
and `passes_algorithm` are identical across all 1260 trials, so a single
pass/fail axis is used.

| Model | Pass rate | Avg quality | Median completion tok | Median wall | Qualifies? |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | **99% (178/180)** | 3.90 | 248 | 17.8s | ✅ |
| `glm-5.1:cloud` | **99% (178/180)** | 4.16 | **145** | **16.1s** | ✅ |
| `deepseek-v4-pro:cloud` | 98% (177/180) | 4.13 | 257 | 17.4s | ✅ |
| `kimi-k2.6:cloud` | 98% (177/180) | **4.31** | 514 | 19.9s | ✅ |
| `minimax-m3:cloud` 🆕 | 98% (176/180) | 4.14 | 249 | 18.6s | ✅ |
| `minimax-m2.7:cloud` | 97% (174/180) | 3.74 | 273 | 19.4s | ✅ |
| `nemotron-3-super:cloud` 🆕 | 93% (168/180) | 3.92 | 480 | 17.7s | ✅ |

**`glm-5.1:cloud` is the standout all-rounder:** tied-best 99% pass, second-best
quality (4.16), and **the most token-efficient by a wide margin** (median 145
completion tokens — roughly half of everyone else, and ~3.5× leaner than
`kimi`'s 514) at the fastest median wall. `kimi` has the top judge-quality but
is the most verbose (and is the judge — see the caveat).

### The two new candidates

- **`minimax-m3:cloud` — a competitive addition.** 98% pass (176/180, level
  with `kimi` and `deepseek-v4-pro`), solid 4.14 quality, and notably efficient
  (249 median tokens). Its 4 misses spread across cpp/go/rust with no single
  weak language. A viable coder-role model.
- **`nemotron-3-super:cloud` — the clear laggard.** 93% pass (168/180) is the
  cohort floor; 12 misses concentrated in csharp (27/30), go (26/30) and rust
  (26/30). Quality (3.92) and verbosity (480 tok) are middling. **Not
  recommended** for the coder role over the incumbents on this evidence.

---

## Discrimination — every question carries signal

Unlike mlang, **no question is all-fail**: all 180 were solved by ≥1 model, so
full == normalized. The few questions that *did* trip models cluster in `go`
and `rust` and in string/whitespace edge-cases — the real discriminators:

| Question | Lang | Passed | Note |
|---|---|---|---|
| `rust-easy-24-longest-word` | rust | **2/7** | hardest in the suite — only ds-pro/glm-class handling of the tie-break + I/O survived |
| `csharp-easy-23-sum-even` | csharp | 5/7 | — |
| `go-easy-03-count-vowels` | go | 5/7 | — |
| `go-easy-14-average` | go | 5/7 | float formatting |
| `go-easy-26-count-evens` | go | 5/7 | — |
| `rust-easy-01-sum-list` | rust | 5/7 | — |

Everything else was solved by 6/7 or 7/7. `c` and `python` were **30/30 for
every model** — fully saturated, zero discrimination.

---

## Pass-rate matrix (n / 30 per language)

| Model | c | cpp | csharp | go | python | rust |
|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 30 | 30 | 30 | **30** | 30 | 28 |
| `deepseek-v4-pro:cloud` | 30 | 29 | 30 | 29 | 30 | 29 |
| `glm-5.1:cloud` | 30 | 30 | 30 | 29 | 30 | 29 |
| `kimi-k2.6:cloud` | 30 | 30 | 30 | 29 | 30 | 28 |
| `minimax-m2.7:cloud` | 30 | 30 | 29 | 25 | 30 | **30** |
| `minimax-m3:cloud` | 30 | 29 | 30 | 28 | 30 | 29 |
| `nemotron-3-super:cloud` | 30 | 29 | 27 | 26 | 30 | 26 |

`go` and `rust` are where the cohort spreads (25–30). Note the spiky
specialisation: `minimax-m2.7` is **worst at go (25)** but the **only model
perfect at rust (30)**.

---

## Per-language verdict

Winners are computed on normalized (= full here) pass-rate, ties broken on
`avg_quality` then `median_tokens`. The **basis** column is the honest signal:
*pass* = correctness-separated; *quality* = pass-saturated, decided on the
judge's 1–5 score (single-judge caveat applies).

| Lang | Winner | Basis | Margin | vs. adopted route ([`coder_defaults.py`](../../consultants/engine/coder_defaults.py)) |
|---|---|---|---|---|
| **go** | `deepseek-v4-flash:cloud` | **pass** (30/30, sole perfect) | clear (others 25–29) | route is `kimi-k2.6` (29/30) — easy gives a test-based pick |
| **rust** | `minimax-m2.7:cloud` | **pass** (30/30, sole perfect) | narrow (others 26–29) | route is `deepseek-v4-flash` (28/30) — easy gives a test-based pick |
| **c** | `kimi-k2.6:cloud` | quality (all 30/30) | 4.52 vs glm 4.38 | route is `glm-5.1`; both 30/30, glm far leaner |
| **csharp** | `kimi-k2.6:cloud` | quality (sat.) | 4.40 vs ds-pro 4.27 | route is `deepseek-v4-pro` (also 30/30) |
| **cpp** | `kimi-k2.6:cloud` ≈ `deepseek-v4-pro` | quality (sat., **tie 4.50**) | tie | route is `deepseek-v4-flash` (30/30) |
| **python** | `glm-5.1:cloud` | quality (all 30/30) | 4.83 (highest) | route is `glm-5.1` ✅ confirmed |

**What the easy suite actually establishes:**

1. **All seven models clear a basic-coding competence floor in all six
   languages** (≥ 25/30 everywhere; ≥ 29/30 for the top five). For c/cpp/csharp/
   python, *any* of the strong models is a safe route — pick on cost/latency.
2. **`glm-5.1` is the best general default** — top pass-rate, near-top quality,
   far the most efficient. This **confirms** the existing
   `RECOMMENDED_CODER_DEFAULT_ROUTE = glm-5.1:cloud`.
3. **go and rust have test-based reliability winners** here (`deepseek-v4-flash`
   and `minimax-m2.7`), which mlang could not provide (both were
   *inconclusive / quality-only* there). The margins are small (1–5 questions on
   easy problems), so treat this as a *reliability* signal, not proof of
   superiority on hard go/rust — weigh it alongside mlang before re-routing.

> **Routes in `coder_defaults.py` are NOT changed by this page** — they are
> mirrored and annotated. Realigning `go → deepseek-v4-flash` and
> `rust → minimax-m2.7` (and reconciling the mlang `c`-route caveat) is a
> separate decision; see [Follow-ups](#follow-ups).

---

## Follow-ups

- **De-bias the saturated languages.** Re-run with a cross-judge panel so the
  c/cpp/csharp/python quality picks aren't single-judge:
  `--audit-judge-model glm-5.1:cloud --meta-judge-model gemma4:31b-cloud`.
- **Tune difficulty for mid-band discrimination.** Neither suite cleanly ranks
  the *top* models per language — mlang saturates at *too hard* (all-fail),
  easy saturates at *too easy* (all-pass). A medium tier sitting between them
  would separate the leaders on correctness rather than quality.
- **Consider the go/rust route realignment** above, weighing easy's
  reliability signal against mlang.

---

## Reproduce

Recompute every number on this page from the run's local `trials.jsonl`
(produced by the bench; gitignored — re-run the cohort below to regenerate it):

```bash
python3 - benchmarks/consultants/results/2026-06-03/coder_easy/trials.jsonl <<'PY'
import json, sys, statistics
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
lang=lambda q: q.split('-',1)[0]
models=sorted({r['model'] for r in rows})
for m in models:
    mr=[r for r in rows if r['model']==m]
    p=sum(1 for r in mr if r['passes_tests'])
    q=[r['quality_score'] for r in mr if r.get('quality_score') is not None]
    print(f"{m:24} {p}/180 {100*p//180}%  avgQ={statistics.mean(q):.2f}")
PY
```

Render the per-language scoreboard from the merged run:

```bash
python benchmarks/consultants/analyze.py \
    benchmarks/consultants/results/2026-06-03/coder_easy/trials.jsonl --by-language
```

Re-run the full live bench (7-model cohort, ~66M Ollama-Pro tokens; run
per-model in parallel into isolated dirs to bound wall-clock — the judge is
the shared bottleneck):

```bash
python benchmarks/consultants/coder_bench.py --live --accept-cost \
    --questions-dir benchmarks/consultants/questions/coder_easy \
    --models glm-5.1:cloud,kimi-k2.6:cloud,deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud,minimax-m3:cloud,nemotron-3-super:cloud \
    --ollama-base http://192.168.178.2:11433 --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from `76440a746bdd`, the question content drifted
without a SUITE.md version bump — investigate before comparing.

---

## Related

- [Benchmark index](index.md) — all benchmark families.
- [Coder per-language (hard tier)](coder-mlang-results.md) — the `coder_mlang`
  sibling; why its per-language picks are quality-only.
- [Coder (Python) suite](../consultants-skill-eval-baselines.md#coder) — the
  single-language suite that anchors the global default.
- [Skill-eval protocol](../consultants-skill-eval-protocol.md) — methodology +
  rubric for all skill-eval suites.
