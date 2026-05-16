---
id: c-medium-01-longest-valid-parens
tier: medium
source: leetcode-32
task: "Length of the longest valid parentheses substring in stdin's line of '(' and ')'. O(N)."
sandbox_path: solution.c
oracle: c-medium-01-longest-valid-parens-oracle.py
---

# c-medium-01-longest-valid-parens

Given a string containing only the characters `(` and `)`, find
the length of the **longest valid (well-formed) parentheses
substring**.

A valid parens substring is one where every `(` has a matching
`)` after it and vice versa, and the matching is nested
(canonical balanced parens). The substring must be **contiguous**
within the input.

## I/O contract

Stdin: a single line — the input string (possibly empty,
possibly up to 30_000 characters).

Stdout: a single integer on its own line — the length of the
longest valid substring.

## Constraints

- `0 <= len(s) <= 30_000`
- Must run in `O(N)` time and `O(N)` extra space (stack or DP).
  An `O(N^2)` brute-force enumeration TLEs on the 30_000-char
  adversarial input.
- Single file `solution.c`. Compiled via
  `gcc -O2 -Wall -o sol solution.c -lm`.

## Examples

```
$ echo "(()" | ./sol
2

$ echo ")()())" | ./sol
4

$ echo "" | ./sol
0

$ echo "()((((((((((" | ./sol
2

$ echo "((((((((((()))" | ./sol
6
```

(In the last case: only `((()))` is balanced — length 6 — even
though there are 11 `(`s, only 3 of them match.)

## Adversarial cases

- Empty string → 0
- All `(` or all `)` → 0
- Alternating `()()()...` of length 30_000 → 30_000
- Deeply nested `((((....))))` of depth 15_000 → 30_000
- A long unbalanced run followed by a balanced suffix — the
  unbalanced prefix must NOT contaminate the suffix count.
