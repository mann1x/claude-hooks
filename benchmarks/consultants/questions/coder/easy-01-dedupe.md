---
id: easy-01-dedupe
tier: easy
source: humaneval/26
task: Write a function `dedupe(items: list) -> list` in a file named `dedupe.py` that returns a new list with all duplicate values removed, preserving the order of the first occurrence of each value. Do not modify the input list. Handle mixed-type lists (strings + ints) as long as the elements are hashable.
sandbox_path: dedupe.py
oracle: easy-01-dedupe-oracle.py
notes: |
  Adapted from HumanEval/26. Variations checked:
  - Preserves insertion order (the trap: a naive `list(set(items))`
    is wrong because set ordering is undefined).
  - Mixed-type with hashable elements.
  - Does NOT mutate the input.
---

# easy-01: stable dedupe

Tests order preservation and the input-mutation-is-bad rule. The
naive `list(set(items))` solution fails the ordering test.
