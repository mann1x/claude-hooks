---
id: hard-02-digit-filter
tier: hard
source: humaneval/146-style
task: Write a function `sum_odd_first_last(nums: list[int]) -> int` in a file named `digit_filter.py` that returns the count of elements in `nums` where BOTH the FIRST and the LAST decimal digit are odd. Only consider elements where the value is strictly greater than 10. Negative numbers: take the absolute value before extracting digits (so `-19` has first digit 1 and last digit 9, both odd; but `-19` is NOT > 10 because we compare the original signed value, so it would be excluded). Return 0 for an empty list.
sandbox_path: digit_filter.py
oracle: hard-02-digit-filter-oracle.py
notes: |
  Adapted from HumanEval/146 with extra traps:
  - "Strictly greater than 10" — borderline value 10 is excluded;
    11 is included.
  - Negative numbers: the > 10 check is on the original value, but
    digit extraction uses abs(). The model must read carefully.
  - Single-digit numbers (1-10) are excluded because they're <= 10.
  - Return type is int.
---

# hard-02: count "double-odd" multi-digit positives

A spec with three independent conditions a careless implementation
will mix up. The oracle covers each interaction explicitly so the
report shows exactly which condition the model violated.
