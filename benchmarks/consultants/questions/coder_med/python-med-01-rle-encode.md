---
id: python-med-01-rle-encode
tier: medium
source: coder_med/rle-encode
sandbox_path: solution.py
oracle: python-med-01-rle-encode-oracle.py
language: python
task: |
  Write a Python program in a file named `solution.py` that reads from standard input and writes the answer to standard output.

  Run-length encode a string: replace each maximal run of one repeated character with that character followed by the length of the run. Runs reset when the character changes.

  Input: one line of lowercase letters (no spaces).
  Output: the run-length encoding on one line.

  Examples (stdin -> stdout):
    'aaabbc\n' -> 'a3b2c1'
    'abc\n' -> 'a1b1c1'
    'aaaa\n' -> 'a4'
    'a\n' -> 'a1'
    'aabbaa\n' -> 'a2b2a2'
    'xyyz\n' -> 'x1y2z1'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# python-med-01-rle-encode — Run-length encode

Run-length encode a string: replace each maximal run of one repeated character with that character followed by the length of the run. Runs reset when the character changes.

- **Input:** one line of lowercase letters (no spaces)
- **Output:** the run-length encoding on one line

## Examples

| stdin | stdout |
|---|---|
| `'aaabbc\n'` | `'a3b2c1'` |
| `'abc\n'` | `'a1b1c1'` |
| `'aaaa\n'` | `'a4'` |
| `'a\n'` | `'a1'` |
| `'aabbaa\n'` | `'a2b2a2'` |
| `'xyyz\n'` | `'x1y2z1'` |
