---
id: c-easy-19-dedupe-order
tier: easy
source: coder_easy/dedupe-order
sandbox_path: solution.c
oracle: c-easy-19-dedupe-order-oracle.py
language: c
task: |
  Write a C program in a file named `solution.c` that reads from standard input and writes the answer to standard output.

  Read a list of integers and print them with duplicates removed, keeping the first occurrence of each value, separated by single spaces.

  Input: whitespace-separated integers.
  Output: the de-duplicated integers, space-separated, in first-seen order.

  Examples (stdin -> stdout):
    '1 2 1 3 2\n' -> '1 2 3'
    '5 5 5\n' -> '5'
    '1 2 3\n' -> '1 2 3'
    '3 1 3 2 1\n' -> '3 1 2'
    '7\n' -> '7'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# c-easy-19-dedupe-order — Deduplicate preserving order

Read a list of integers and print them with duplicates removed, keeping the first occurrence of each value, separated by single spaces.

- **Input:** whitespace-separated integers
- **Output:** the de-duplicated integers, space-separated, in first-seen order

## Examples

| stdin | stdout |
|---|---|
| `'1 2 1 3 2\n'` | `'1 2 3'` |
| `'5 5 5\n'` | `'5'` |
| `'1 2 3\n'` | `'1 2 3'` |
| `'3 1 3 2 1\n'` | `'3 1 2'` |
| `'7\n'` | `'7'` |
