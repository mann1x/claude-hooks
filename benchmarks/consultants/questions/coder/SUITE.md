---
suite: coder
suite_version: "1.0"
released: 2026-05-16
manifest:
  - trivial-01-truncate
  - trivial-02-strlen
  - easy-01-dedupe
  - easy-02-fib
  - medium-01-balance
  - medium-02-prime-length
  - hard-01-matrix-path
  - hard-02-digit-filter
rubric:
  pass_rate_floor: 0.70
  quality_score_floor: 3.5
  tie_breaker: median_tokens
---

# Coder Skill-Eval Suite v1.0

This file is the **manifest** for the coder protocol of the
**Consultancy Skill-Eval Protocol**. See
[`docs/consultants-skill-eval-protocol.md`](../../../../docs/consultants-skill-eval-protocol.md)
for the methodology, when-to-rerun rules, and the decision-rubric
context.

## What this suite measures

A candidate model's fitness for the M10 `coder` role: can it write
correct, idiomatic Python given a one-paragraph natural-language
task + a sandboxed `write_file` tool with byte caps? Trials are
binary on correctness (pytest passes/fails) and 1-5 on quality
(LLM-judged against a strict rubric).

## Manifest (8 questions × 4 tiers)

| Tier    | ID                     | Source                  | Trap                                                      |
|---------|------------------------|-------------------------|-----------------------------------------------------------|
| trivial | trivial-01-truncate    | humaneval-style/custom  | Edge cases (n=0, n<0, overflow)                           |
| trivial | trivial-02-strlen      | humaneval/23 (modified) | "no len()" constraint must be read + honored              |
| easy    | easy-01-dedupe         | humaneval/26            | `list(set(items))` breaks order — must be stable          |
| easy    | easy-02-fib            | humaneval/55            | Naive recursion fails the fib(40) timeout                 |
| medium  | medium-01-balance      | humaneval-style/custom  | Counter approach fails on `"([)]"` — stack required       |
| medium  | medium-02-prime-length | humaneval/82            | 0/1 not prime; 2 IS prime — easy off-by-one               |
| hard    | hard-01-matrix-path    | classic DP              | Naive recursion fails 10×10 timeout; negatives allowed    |
| hard    | hard-02-digit-filter   | humaneval/146-style     | "> 10" boundary + signed-vs-abs digit extraction          |

## Rubric (the decision)

A model **qualifies for the coder role default** iff:

1. `pass_rate ≥ 0.70` — at least 6 of 8 oracle test suites pass.
2. `avg_quality_score ≥ 3.5` — LLM judge averages 3.5 or better
   across the trials that compiled (judge isn't run on broken code).

Among qualifying models, the **recommended default** is the one
with the highest `pass_rate`. Ties break on `median_tokens`
(cheaper wins). If no model qualifies, the role stays at the
project-global `DEFAULT_MODEL` and a follow-up commit re-runs the
suite with a relaxed rubric or against a different candidate set
— **never** silently flip the default on a sub-threshold model.

## Reproducibility

Each results file records `suite_version: "1.0"` + the manifest
hash. Re-running v1.0 of the suite against a model that was scored
in a prior run should produce statistically similar results
(modulo proxy-side flap / model-side temperature drift). If you
see a >15-pt swing on the same model between two v1.0 runs,
investigate the proxy upstream before trusting the new number.

## Versioning

The suite version follows semver-ish rules:

- **PATCH** (1.0 → 1.0.1): a question's oracle gets stricter
  pytest assertions but the spec is unchanged. Old baselines stay
  comparable; report any score drift as model-side, not
  suite-side.
- **MINOR** (1.0 → 1.1): a new question is added. Baselines from
  v1.0 are still valid for the v1.0 manifest, but a v1.1 score is
  computed on a different denominator.
- **MAJOR** (1.0 → 2.0): question added/removed AND rubric
  threshold or weighting changed. Baselines from v1.x are NOT
  directly comparable to v2.x — a re-baseline run is required.

## Adding a question (suite v1.1+)

1. Create `<tier>-<NN>-<short-id>.md` with frontmatter matching
   the schema in [`harness.py`](../../harness.py)'s `BenchQuestion`.
2. Create `<tier>-<NN>-<short-id>-oracle.py` — pytest file
   importing from `$CODER_SANDBOX`.
3. Append the new id to the `manifest:` list in this file +
   bump `suite_version` to the next MINOR.
4. Re-baseline every previously-scored model on the new suite
   version (one commit, results land under
   `docs/consultants-skill-eval-baselines.md`).
5. The rubric thresholds STAY at 0.70 / 3.5 unless explicitly
   discussed (any threshold change is MAJOR).
