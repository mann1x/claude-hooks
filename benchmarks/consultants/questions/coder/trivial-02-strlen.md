---
id: trivial-02-strlen
tier: trivial
source: humaneval/23
task: Write a function `strlen(s: str) -> int` in a file named `strlen.py` that returns the number of characters in `s`. Do NOT call the built-in `len()` function or any other helper that returns a string's length directly. Implement the count manually (e.g. iterate, or use `sum(1 for _ in s)`).
sandbox_path: strlen.py
oracle: trivial-02-strlen-oracle.py
notes: |
  Adapted from HumanEval/23. Difference: the original problem
  allowed `len()`. We forbid it here because the bench is testing
  the model's ability to read constraints carefully, not its
  ability to call a builtin.
---

# trivial-02: string length without len()

Tests whether the model reads the "do not use len()" constraint.
Oracle covers basic strings, empty string, single character, and
unicode (one codepoint per character).
