---
id: c-med-02-balanced-brackets
tier: medium
source: coder_med/balanced-brackets
sandbox_path: solution.c
oracle: c-med-02-balanced-brackets-oracle.py
language: c
task: |
  Write a C program in a file named `solution.c` that reads from standard input and writes the answer to standard output.

  Decide whether a string of brackets is balanced. The three bracket pairs are (), [], and {}. Every opening bracket must be closed by the matching kind in the correct order.

  Input: one line containing only the characters ()[]{} (possibly empty).
  Output: YES if balanced, otherwise NO.

  Examples (stdin -> stdout):
    '()[]{}\n' -> 'YES'
    '([{}])\n' -> 'YES'
    '([)]\n' -> 'NO'
    '(((\n' -> 'NO'
    ')(\n' -> 'NO'
    '\n' -> 'YES'
    '{[()()]}\n' -> 'YES'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# c-med-02-balanced-brackets — Balanced brackets

Decide whether a string of brackets is balanced. The three bracket pairs are (), [], and {}. Every opening bracket must be closed by the matching kind in the correct order.

- **Input:** one line containing only the characters ()[]{} (possibly empty)
- **Output:** YES if balanced, otherwise NO

## Examples

| stdin | stdout |
|---|---|
| `'()[]{}\n'` | `'YES'` |
| `'([{}])\n'` | `'YES'` |
| `'([)]\n'` | `'NO'` |
| `'(((\n'` | `'NO'` |
| `')(\n'` | `'NO'` |
| `'\n'` | `'YES'` |
| `'{[()()]}\n'` | `'YES'` |
