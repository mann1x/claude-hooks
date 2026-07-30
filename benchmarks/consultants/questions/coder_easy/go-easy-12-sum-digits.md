---
id: go-easy-12-sum-digits
tier: easy
source: coder_easy/sum-digits
sandbox_path: solution.go
oracle: go-easy-12-sum-digits-oracle.py
language: go
task: |
  Write a Go program in a file named `solution.go` that reads from standard input and writes the answer to standard output.

  Read a non-negative integer (as text) and print the sum of its decimal digits.

  Input: a single non-negative integer, possibly very large.
  Output: a single integer: the digit sum.

  Examples (stdin -> stdout):
    '123\n' -> '6'
    '0\n' -> '0'
    '9999\n' -> '36'
    '1000000\n' -> '1'
    '505\n' -> '10'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# go-easy-12-sum-digits — Sum of digits

Read a non-negative integer (as text) and print the sum of its decimal digits.

- **Input:** a single non-negative integer, possibly very large
- **Output:** a single integer: the digit sum

## Examples

| stdin | stdout |
|---|---|
| `'123\n'` | `'6'` |
| `'0\n'` | `'0'` |
| `'9999\n'` | `'36'` |
| `'1000000\n'` | `'1'` |
| `'505\n'` | `'10'` |
