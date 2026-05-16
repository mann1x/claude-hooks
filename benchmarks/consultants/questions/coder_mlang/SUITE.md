# Coder Multi-Language Suite v1.0

Per-language stress test of the coder role. Question content
ported from LCB / LeetCode-hard-tier algorithmic problems and
language-idiomatic engineering challenges. See
[`docs/consultants-skill-eval-mlang-suite.md`](../../../../docs/consultants-skill-eval-mlang-suite.md)
for the design + rationale.

The v1.0 manifest deliberately strips **all** "Hint" sections
and tightens "Required structure" blocks to API contracts only
— the v1 `coder@1.0` suite saturated because its specs read
like tutorials. Models here must reason about the
problem, not transcribe the prompt.

## Suite metadata

```yaml
suite: coder_mlang
suite_version: 1.0
released: 2026-05-16
languages:
  - python
  - rust
  - go
  - c
  - cpp
  - csharp
tiers:
  - medium
  - hard
  - very_hard
```

## Rubric

```yaml
pass_rate_floor: 0.70
quality_score_floor: 3.5
tie_breaker: median_tokens
per_language_decision: true
global_decision_tie_break: python
```

`per_language_decision = true` means the suite recommends one
default model **per language** in addition to the global pick.
The decision lands in
`consultants/engine/coder_defaults.py:RECOMMENDED_CODER_MODEL_BY_LANGUAGE`.

## Manifest

```yaml
questions:
  # ----- medium tier (LCB-derived algorithmic problems) -----
  - id: python-medium-01-meeting-rooms
    tier: medium
    language: python
    sandbox_path: solution.py
    task: python-medium-01-meeting-rooms.md
    oracle: oracle_python-medium-01-meeting-rooms.py
  - id: rust-medium-01-search-rotated
    tier: medium
    language: rust
    sandbox_path: solution.rs
    task: rust-medium-01-search-rotated.md
    oracle: oracle_rust-medium-01-search-rotated.py
  - id: go-medium-01-koko-bananas
    tier: medium
    language: go
    sandbox_path: solution.go
    task: go-medium-01-koko-bananas.md
    oracle: oracle_go-medium-01-koko-bananas.py
  - id: c-medium-01-longest-valid-parens
    tier: medium
    language: c
    sandbox_path: solution.c
    task: c-medium-01-longest-valid-parens.md
    oracle: oracle_c-medium-01-longest-valid-parens.py
  - id: cpp-medium-01-cycle-list
    tier: medium
    language: cpp
    sandbox_path: solution.cpp
    task: cpp-medium-01-cycle-list.md
    oracle: oracle_cpp-medium-01-cycle-list.py
  - id: csharp-medium-01-trapped-rainwater
    tier: medium
    language: csharp
    sandbox_path: solution.cs
    task: csharp-medium-01-trapped-rainwater.md
    oracle: oracle_csharp-medium-01-trapped-rainwater.py

  # ----- hard tier (engineering challenges + LCB-hard) -----
  - id: python-hard-01-lru-cache
    tier: hard
    language: python
    sandbox_path: solution.py
    task: python-hard-01-lru-cache.md
    oracle: oracle_python-hard-01-lru-cache.py
  - id: rust-hard-01-iter-window-pairs
    tier: hard
    language: rust
    sandbox_path: solution.rs
    task: rust-hard-01-iter-window-pairs.md
    oracle: oracle_rust-hard-01-iter-window-pairs.py
  - id: go-hard-01-shortest-path-k-stops
    tier: hard
    language: go
    sandbox_path: solution.go
    task: go-hard-01-shortest-path-k-stops.md
    oracle: oracle_go-hard-01-shortest-path-k-stops.py
  - id: c-hard-01-quicksort-3way
    tier: hard
    language: c
    sandbox_path: solution.c
    task: c-hard-01-quicksort-3way.md
    oracle: oracle_c-hard-01-quicksort-3way.py
  - id: cpp-hard-01-expr-eval
    tier: hard
    language: cpp
    sandbox_path: solution.cpp
    task: cpp-hard-01-expr-eval.md
    oracle: oracle_cpp-hard-01-expr-eval.py
  - id: csharp-hard-01-async-debounce
    tier: hard
    language: csharp
    sandbox_path: solution.cs
    task: csharp-hard-01-async-debounce.md
    oracle: oracle_csharp-hard-01-async-debounce.py

  # ----- very_hard tier (data-structure design + concurrency) -----
  - id: python-very_hard-01-parser-combinator
    tier: very_hard
    language: python
    sandbox_path: solution.py
    task: python-very_hard-01-parser-combinator.md
    oracle: oracle_python-very_hard-01-parser-combinator.py
  - id: rust-very_hard-01-bank-transfer
    tier: very_hard
    language: rust
    sandbox_path: solution.rs
    task: rust-very_hard-01-bank-transfer.md
    oracle: oracle_rust-very_hard-01-bank-transfer.py
  - id: go-very_hard-01-spsc-queue
    tier: very_hard
    language: go
    sandbox_path: solution.go
    task: go-very_hard-01-spsc-queue.md
    oracle: oracle_go-very_hard-01-spsc-queue.py
  - id: c-very_hard-01-rbtree-insert
    tier: very_hard
    language: c
    sandbox_path: solution.c
    task: c-very_hard-01-rbtree-insert.md
    oracle: oracle_c-very_hard-01-rbtree-insert.py
  - id: cpp-very_hard-01-small-vector
    tier: very_hard
    language: cpp
    sandbox_path: solution.cpp
    task: cpp-very_hard-01-small-vector.md
    oracle: oracle_cpp-very_hard-01-small-vector.py
  - id: csharp-very_hard-01-di-container
    tier: very_hard
    language: csharp
    sandbox_path: solution.cs
    task: csharp-very_hard-01-di-container.md
    oracle: oracle_csharp-very_hard-01-di-container.py
```

## Provenance of the question set

Of the 18 questions in v1.0:

- 8 are **LCB / LeetCode-derived** algorithmic problems with no
  implementation hints in the spec: meeting-rooms,
  search-rotated, koko-bananas, longest-valid-parens, cycle-list,
  trapped-rainwater, shortest-path-k-stops, lru-cache.
- 10 are **language-idiom engineering challenges**: iterator
  adapters (Rust), recursive descent (C++), red-black tree (C),
  3-way quicksort (C), parser combinators (Python), thread-safe
  bank transfer (Rust), SPSC queue (Go), async debounce (C#),
  DI container (C#), SmallVector storage (C++).

Both categories were drafted with **no implementation guidance
in the prompt** — `Hint` sections explicitly omitted, `Required
structure` blocks tightened to API contracts only. Adversarial
test cases (large input that breaks naïve `O(N^2)`, deadlock-
prone access patterns, edge cases like empty input + boundary
overflow) are pinned in every oracle.

The full design rationale + the 2026-05-16 scoping decisions
(why these 18 specifically, why drop trivial/easy tiers, why
the 5-model cohort) live in
[`docs/consultants-skill-eval-mlang-suite.md`](../../../../docs/consultants-skill-eval-mlang-suite.md).
