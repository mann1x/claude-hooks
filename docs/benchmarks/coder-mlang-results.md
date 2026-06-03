# Coder skill-eval — per-language results (`coder_mlang` v1.0.1)

> ↟ [Benchmark index](index.md) · suite design:
> [`consultants-skill-eval-mlang-suite.md`](../consultants-skill-eval-mlang-suite.md) ·
> ledger row: [`consultants-skill-eval-baselines.md`](../consultants-skill-eval-baselines.md#v101-mlang-baseline-2026-05-17--per-language-winners) ·
> adopted routes in code:
> [`consultants/engine/coder_defaults.py`](../../consultants/engine/coder_defaults.py).

This page answers **"which cloud model is the best coder for each language?"**
and shows *how* the per-language picks were derived — the detail that was
previously only implicit in `coder_defaults.py`.

---

## ⚠️ Read this first — the suite is intentionally brutal, so scores are normalized

`coder_mlang@1.0.1` drops the easy tiers and keeps only **medium / hard /
very_hard** problems, specifically to break the ceiling where the Python-only
`coder@1.0` suite had three models tied at ~100% pass. The side effect: on the
**strict full-oracle axis, no model qualifies** — the headline run shows
`pass_rate` between 8% and 15% and the
[run `report.md`](../../benchmarks/consultants/results/2026-05-17/coder_mlang-v1.0.1/report.md)
correctly says *"no model meets the rubric."*

That headline is **not** the basis for the per-language picks, and reading it
alone is misleading. **10 of the 13 questions defeat every model in the cohort**
— they don't discriminate, so they tell you nothing about *relative* skill. The
classification below therefore uses **normalized** scoring:

> **Normalization rule:** a question counts toward the per-model / per-language
> ranking only if **at least one** model passed it. Questions where **all five
> models fail** are excluded from the classification (they measure the suite's
> difficulty, not the models). Both the **full** (all 13 questions) and
> **normalized** (the 3 discriminating questions) numbers are shown so nothing
> is hidden.

In this run the two oracle axes coincide: **`passes_tests` (strict, full oracle)
and `passes_algorithm` (algorithm-only) are identical for all 65 trials**, so a
single pass/fail axis is used throughout.

**Provenance:** `coder_mlang` v1.0.1, suite hash `ddef8095`, judge
`kimi-k2.6:cloud`, git `07ff79c`, host solidpc, 2026-05-17. 13 questions × 5
models = 65 trials. Source of truth:
[`trials.jsonl`](../../benchmarks/consultants/results/2026-05-17/coder_mlang-v1.0.1/)
(git-present). Numbers below are recomputed directly from it — see
[Reproduce](#reproduce).

---

## Per-model summary — full vs normalized

Rubric (full axis): `pass_rate ≥ 70%` AND `avg_quality ≥ 3.5`. **No model
qualifies on the full axis.** Normalized = over the 3 discriminating questions
only.

| Model | pass% full (n=13) | pass% **normalized** (n=3) | avgQ full | avgQ **normalized** | median tokens | median wall |
|---|---|---|---|---|---|---|
| `kimi-k2.6:cloud` | 15% (2/13) | **67% (2/3)** | 3.69 | **4.00** | 4719 | 70.8s |
| `minimax-m2.7:cloud` | 15% (2/13) | 67% (2/3) | 2.15 | 2.67 | 3541 | 25.7s |
| `deepseek-v4-flash:cloud` | 8% (1/13) | 33% (1/3) | 3.08 | 3.67 | 3704 | 22.7s |
| `deepseek-v4-pro:cloud` | 8% (1/13) | 33% (1/3) | 2.77 | 3.67 | 3345 | 17.5s |
| `glm-5.1:cloud` | 8% (1/13) | 33% (1/3) | 2.83 | 3.50 | 3218 | 11.2s |

On the answerable questions, **`kimi-k2.6:cloud` is the cohort leader** (67%
normalized pass, 4.00 normalized quality); `minimax` ties on pass but trails
badly on quality. `glm-5.1` is the fastest/cheapest but lowest normalized pass.

---

## Question discrimination — which questions tell us anything

13 questions; only **3 discriminate** (≥1 model passed). The other 10 (every
very_hard, plus the cpp/csharp/python/rust hard problems) defeated all five
models and are **excluded** from classification.

| Question | Lang | Tier | Passes | Who passed |
|---|---|---|---|---|
| `csharp-medium-01-trapped-rainwater` | csharp | medium | **4/5** | kimi, minimax, ds-pro, glm |
| `c-hard-01-quicksort-3way` | c | hard | **2/5** | kimi, minimax |
| `cpp-medium-01-cycle-list` | cpp | medium | **1/5** | ds-flash |
| `c-very_hard-01-rbtree-insert` | c | very_hard | 0/5 | — (all fail, excluded) |
| `cpp-hard-01-expr-eval` | cpp | hard | 0/5 | — (all fail, excluded) |
| `cpp-very_hard-01-small-vector` | cpp | very_hard | 0/5 | — (all fail, excluded) |
| `csharp-hard-01-async-debounce` | csharp | hard | 0/5 | — (all fail, excluded) |
| `csharp-very_hard-01-di-container` | csharp | very_hard | 0/5 | — (all fail, excluded) |
| `go-very_hard-01-spsc-queue` | go | very_hard | 0/5 | — (all fail, excluded) |
| `python-hard-01-lru-cache` | python | hard | 0/5 | — (all fail, excluded) |
| `python-very_hard-01-parser-combinator` | python | very_hard | 0/5 | — (all fail, excluded) |
| `rust-hard-01-iter-window-pairs` | rust | hard | 0/5 | — (all fail, excluded) |
| `rust-very_hard-01-bank-transfer` | rust | very_hard | 0/5 | — (all fail, excluded) |

**Consequence:** `go`, `python`, and `rust` have **zero** discriminating
questions — *every* question in those three languages defeated *every* model.
There is **no test-based winner** for them in this run; any per-language pick
there is a **quality-judge-only** ranking (the judge's 1–5 opinion of code that
does not pass the tests), not a demonstrated capability.

---

## Per-language verdict

Computed over discriminating questions only. "Quality-only" languages have no
passing trial, so the column shows the judge-quality ranking with an explicit
*inconclusive* flag.

| Lang | Discriminating Q | Test-winner(s) | Adopted primary ([`coder_defaults.py`](../../consultants/engine/coder_defaults.py)) | Status |
|---|---|---|---|---|
| **csharp** | `csharp-medium-01` | kimi, minimax, ds-pro, glm (4-way) | `deepseek-v4-pro:cloud` | ✅ test-validated (a passer) |
| **cpp** | `cpp-medium-01` | deepseek-v4-flash | `deepseek-v4-flash:cloud` | ✅ test-validated |
| **c** | `c-hard-01` | kimi, minimax | `glm-5.1:cloud` | ⚠️ **mismatch** — glm **failed** the only discriminating C question |
| **go** | none | — (all fail) | `kimi-k2.6:cloud` | ◓ inconclusive — quality-only (kimi top judge-q 4.0) |
| **python** | none | — (all fail) | `glm-5.1:cloud` | ◓ inconclusive — quality-only (glm/kimi tie judge-q 4.0) |
| **rust** | none | — (all fail) | `deepseek-v4-flash:cloud` | ◓ inconclusive — quality-only (flash/kimi tie judge-q 3.5) |

### The C-route caveat

The adopted `c → glm-5.1:cloud` route is **not** supported by the trial data:
the only discriminating C question (`c-hard-01-quicksort-3way`) was passed by
**kimi and minimax**; glm-5.1 failed it (and the very_hard C question defeated
everyone). The route was chosen for speed/cohort-consistency, per the note in
`baselines.md`. If you want C routed to a **test-validated** model, `kimi-k2.6`
is the evidence-backed pick here. This is a **documentation flag only** — the
code route is unchanged; realigning it is a separate decision.

---

## Adopted routes (as shipped)

Mirrors `RECOMMENDED_CODER_ROUTES_BY_LANGUAGE` + `RECOMMENDED_CODER_DEFAULT_ROUTE`
in [`coder_defaults.py`](../../consultants/engine/coder_defaults.py). `primary →
fallback`; failover fires on any exception, zero files written, or empty final
message.

| Lang | primary | fallback | basis |
|---|---|---|---|
| c | `glm-5.1:cloud` | `deepseek-v4-pro:cloud` | speed/consistency (⚠️ see caveat) |
| cpp | `deepseek-v4-flash:cloud` | `kimi-k2.6:cloud` | test-winner |
| csharp | `deepseek-v4-pro:cloud` | `kimi-k2.6:cloud` | test-passer (top judge-q among passers) |
| go | `kimi-k2.6:cloud` | `deepseek-v4-pro:cloud` | quality-only (inconclusive) |
| python | `glm-5.1:cloud` | `kimi-k2.6:cloud` | quality-only (inconclusive) |
| rust | `deepseek-v4-flash:cloud` | `deepseek-v4-pro:cloud` | quality-only (inconclusive) |
| **default** (any other language) | `glm-5.1:cloud` | `kimi-k2.6:cloud` | v1.0 single-language winner |

> The global default falls back to the Python-only `coder@1.0` winner
> (`glm-5.1:cloud`) — that suite is a cleaner signal for general coding; see
> [baselines → Coder](../consultants-skill-eval-baselines.md#coder).

---

## Reproduce

Recompute every number on this page from the committed trials:

```bash
python3 - benchmarks/consultants/results/2026-05-17/coder_mlang-v1.0.1/trials.jsonl <<'PY'
import json, sys
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
models=sorted({r['model'] for r in rows}); qs=sorted({r['question_id'] for r in rows})
cell={(r['question_id'],r['model']):r for r in rows}
disc=[q for q in qs if any(cell[(q,m)]['passes_tests'] for m in models)]
print("discriminating:", disc)
for m in models:
    full=[cell[(q,m)]['passes_tests'] for q in qs]
    norm=[cell[(q,m)]['passes_tests'] for q in disc]
    qn=[cell[(q,m)]['quality_score'] for q in disc if cell[(q,m)]['quality_score'] is not None]
    print(f"{m:26} full={100*sum(full)//len(full)}% norm={100*sum(norm)//len(norm)}% avgQ_norm={sum(qn)/len(qn):.2f}")
PY
```

Re-run the live bench (5-model cohort, ~270K tokens):

```bash
python benchmarks/consultants/coder_bench.py --live --accept-cost \
    --questions-dir benchmarks/consultants/questions/coder_mlang \
    --models glm-5.1:cloud,kimi-k2.6:cloud,deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud \
    --ollama-base http://192.168.178.2:11433 --judge-model kimi-k2.6:cloud
```

---

## Related

- [Benchmark index](index.md) — all benchmark families.
- [Coder (Python) suite](../consultants-skill-eval-baselines.md#coder) — the
  single-language sibling that anchors the global default.
- [mlang suite design](../consultants-skill-eval-mlang-suite.md) — manifest,
  toolchain pinning, tier semantics.
- [Skill-eval protocol](../consultants-skill-eval-protocol.md) — methodology +
  rubric for all skill-eval suites.
