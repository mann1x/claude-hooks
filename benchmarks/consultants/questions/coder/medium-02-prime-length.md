---
id: medium-02-prime-length
tier: medium
source: humaneval/82
task: Write a function `prime_length(s: str) -> bool` in a file named `prime_length.py` that returns True iff the length of `s` is a prime number. Recall a prime is a positive integer >= 2 with no divisors other than 1 and itself; therefore lengths 0 and 1 should return False. Implement the primality check without depending on `sympy` or any third-party library.
sandbox_path: prime_length.py
oracle: medium-02-prime-length-oracle.py
notes: |
  Adapted from HumanEval/82. Traps:
  - 0 and 1 are NOT prime; some submissions forget this.
  - 2 IS prime (the only even prime).
  - Inefficient O(n) primality is acceptable; the oracle doesn't
    test large inputs.
---

# medium-02: prime-length string

Straightforward primality + a small edge-case trap (length 0, 1).
The oracle uses strings of carefully chosen lengths to exercise
each branch of a typical primality implementation.
