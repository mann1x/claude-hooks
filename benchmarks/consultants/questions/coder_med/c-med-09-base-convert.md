---
id: c-med-09-base-convert
tier: medium
source: coder_med/base-convert
sandbox_path: solution.c
oracle: c-med-09-base-convert-oracle.py
language: c
task: |
  Write a C program in a file named `solution.c` that reads from standard input and writes the answer to standard output.

  Convert a non-negative integer from one base to another. Bases are between 2 and 16; digits a-f are lowercase and represent 10-15. Output uses lowercase digits and has no leading zeros (except the value zero, printed as 0).

  Input: one line: from_base to_base value (value in from_base).
  Output: the value written in to_base.

  Examples (stdin -> stdout):
    '16 2 ff\n' -> '11111111'
    '2 10 1010\n' -> '10'
    '10 16 255\n' -> 'ff'
    '10 2 0\n' -> '0'
    '8 10 17\n' -> '15'
    '10 16 4096\n' -> '1000'
    '16 10 1a\n' -> '26'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# c-med-09-base-convert — Base conversion

Convert a non-negative integer from one base to another. Bases are between 2 and 16; digits a-f are lowercase and represent 10-15. Output uses lowercase digits and has no leading zeros (except the value zero, printed as 0).

- **Input:** one line: from_base to_base value (value in from_base)
- **Output:** the value written in to_base

## Examples

| stdin | stdout |
|---|---|
| `'16 2 ff\n'` | `'11111111'` |
| `'2 10 1010\n'` | `'10'` |
| `'10 16 255\n'` | `'ff'` |
| `'10 2 0\n'` | `'0'` |
| `'8 10 17\n'` | `'15'` |
| `'10 16 4096\n'` | `'1000'` |
| `'16 10 1a\n'` | `'26'` |
