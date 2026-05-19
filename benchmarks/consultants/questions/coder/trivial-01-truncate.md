---
id: trivial-01-truncate
tier: trivial
source: humaneval-style/custom
task: Write a function `truncate(s: str, n: int) -> str` in a file named `truncate.py` that returns the first `n` characters of `s`. If `n` is greater than the length of `s`, return `s` unchanged. If `n <= 0`, return an empty string. Do not use slicing tricks that depend on negative indices; handle the edge cases explicitly.
sandbox_path: truncate.py
oracle: trivial-01-truncate-oracle.py
notes: |
  Baseline trivial — one-liner shouldn't even need iteration. Oracle
  covers: basic case, n == 0, n < 0, n > len(s), empty string input.
---

# trivial-01: truncate to first N characters

The first test in the M11b suite. A model that can't pass this
isn't worth running on harder tiers — the task is intentionally
unambiguous so a 1–2-line implementation passes the oracle.
