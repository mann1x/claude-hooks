---
id: easy-02-fib
tier: easy
source: humaneval/55
task: Write a function `fib(n: int) -> int` in a file named `fib.py` that returns the nth Fibonacci number, where `fib(0) == 0`, `fib(1) == 1`, and `fib(n) == fib(n - 1) + fib(n - 2)` for `n >= 2`. Use iteration (not naive recursion) so the function returns quickly even for `n` up to 50. Raise `ValueError` for negative `n`.
sandbox_path: fib.py
oracle: easy-02-fib-oracle.py
notes: |
  Adapted from HumanEval/55. Two trap conditions:
  - Naive recursion times out for `n=40`.
  - Negative input must raise ValueError (caught by oracle).
---

# easy-02: Fibonacci

Tests for: correct base cases, iteration vs naive recursion (we
run `fib(40)` with a 2-second timeout), and explicit error
handling for negative input.
