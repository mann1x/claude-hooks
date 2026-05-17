---
suite: coder_mlang
suite_version: "1.0.1"
released: 2026-05-17
manifest:
  - python-medium-01-meeting-rooms
  - rust-medium-01-search-rotated
  - go-medium-01-koko-bananas
  - c-medium-01-longest-valid-parens
  - cpp-medium-01-cycle-list
  - csharp-medium-01-trapped-rainwater
  - python-hard-01-lru-cache
  - rust-hard-01-iter-window-pairs
  - go-hard-01-shortest-path-k-stops
  - c-hard-01-quicksort-3way
  - cpp-hard-01-expr-eval
  - csharp-hard-01-async-debounce
  - python-very_hard-01-parser-combinator
  - rust-very_hard-01-bank-transfer
  - go-very_hard-01-spsc-queue
  - c-very_hard-01-rbtree-insert
  - cpp-very_hard-01-small-vector
  - csharp-very_hard-01-di-container
rubric:
  pass_rate_floor: 0.70
  quality_score_floor: 3.5
  tie_breaker: median_tokens
---

# Coder Multi-Language Suite v1.0.1

## v1.0.1 changelog (2026-05-17)

The v1.0 cohort run (2026-05-16, 90 trials, 33% pass) surfaced
that **every failure** traced to one of three modes — none of
the oracles were actually broken:

1. Spec-defined name violation (e.g., model named the function
   `sort` instead of the required `quicksort3`).
2. Spec-defined output-format violation (e.g., model added a
   `In-order:` prefix, used tuple notation `(10, 20)` instead
   of `10 20`, returned `list` where the spec required `tuple`).
3. Genuine compilation/semantic difficulty.

v1.0.1 tightens the **prompts of the 10 questions** with 0/5
pass rate, adding a top-of-file "⚠️ CRITICAL CONSTRAINTS"
block that extracts the spec's verbatim requirements into the
first thing the model sees. The oracles are unchanged — this
is a pure prompt-engineering experiment that separates "can do
the algorithm" from "can read the spec carefully" as cohort
signals.

Affected questions: c-hard-01-quicksort-3way,
c-very_hard-01-rbtree-insert, cpp-hard-01-expr-eval,
cpp-very_hard-01-small-vector, csharp-hard-01-async-debounce,
csharp-very_hard-01-di-container, go-very_hard-01-spsc-queue,
python-very_hard-01-parser-combinator,
rust-hard-01-iter-window-pairs,
rust-very_hard-01-bank-transfer.

Per-language stress test of the coder role. Question content
ported from LCB / LeetCode-hard-tier algorithmic problems and
language-idiomatic engineering challenges. See
[`docs/consultants-skill-eval-mlang-suite.md`](../../../../docs/consultants-skill-eval-mlang-suite.md)
for the design + rationale.

The v1.0 manifest deliberately strips **all** "Hint" sections and
tightens "Required structure" blocks to API contracts only — the
v1 `coder@1.0` suite saturated because its specs read like
tutorials. Models here must reason about the problem, not
transcribe the prompt.

## What this suite measures

A candidate model's fitness for the `coder` role across the
languages the user writes: **Python, Rust, Go, C, C++, C#**.
Six questions per tier × three tiers (medium / hard / very_hard)
= 18 questions × 5-model cohort = 90 trials per full run.

## Manifest (3 tiers × 6 languages = 18 questions)

| Tier        | Python                                | Rust                                   | Go                                          | C                              | C++                          | C#                              |
|-------------|---------------------------------------|----------------------------------------|---------------------------------------------|--------------------------------|------------------------------|---------------------------------|
| medium      | meeting-rooms (LC 253)                | search-rotated (LC 33)                 | koko-bananas (LC 875)                       | longest-valid-parens (LC 32)   | cycle-list (LC 142, Floyd)   | trapped-rainwater (LC 42)       |
| hard        | lru-cache (LC 146)                    | iter-window-pairs (Iterator trait)     | shortest-path-k-stops (LC 787, Dijkstra)    | quicksort-3way (Dutch flag)    | expr-eval (recursive descent)| async-debounce (Task.Delay+CTS) |
| very_hard   | parser-combinator (Seq/Or/Many)       | bank-transfer (Mutex deadlock-free)    | spsc-queue (lock-free atomics)              | rbtree-insert (RB invariants)  | small-vector (placement new) | di-container (reflection+cycle) |

## Rubric (the decision)

Decision rule baked into the YAML frontmatter:

> A model **qualifies** iff `pass_rate ≥ 0.70` AND
> `avg_quality_score ≥ 3.5`. Among qualifying models, the
> **recommended default** is the one with the highest
> `pass_rate`. Ties break on `median_tokens` (cheaper wins).
>
> If no model qualifies, the role's default stays at the
> project-global DEFAULT_MODEL and a follow-up run evaluates a
> different candidate set — never silently flip the default on
> a sub-threshold model.

Per-language decisions (recommended default per language) land
in `consultants/engine/coder_defaults.py:RECOMMENDED_CODER_MODEL_BY_LANGUAGE`.

## Provenance

Of the 18 questions in v1.0:

- 8 are **LCB / LeetCode-derived** algorithmic problems with no
  implementation hints in the prompt: meeting-rooms,
  search-rotated, koko-bananas, longest-valid-parens, cycle-list,
  trapped-rainwater, shortest-path-k-stops, lru-cache.
- 10 are **language-idiom engineering challenges**: iterator
  adapters (Rust), recursive descent (C++), red-black tree (C),
  3-way quicksort (C), parser combinators (Python), thread-safe
  bank transfer (Rust), SPSC queue (Go), async debounce (C#),
  DI container (C#), SmallVector storage (C++).

Both categories ship with NO implementation guidance — `Hint`
sections explicitly omitted, `Required structure` blocks
tightened to API contracts only. Adversarial test cases (large
input that breaks naïve `O(N^2)`, deadlock-prone access
patterns, edge cases like empty input + boundary overflow) are
pinned in every oracle.

The full design rationale + the 2026-05-16 scoping decisions
live in
[`docs/consultants-skill-eval-mlang-suite.md`](../../../../docs/consultants-skill-eval-mlang-suite.md).
