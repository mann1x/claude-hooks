---
id: python-med-10-window-max
tier: medium
source: coder_med/window-max
sandbox_path: solution.py
oracle: python-med-10-window-max-oracle.py
language: python
task: |
  Write a Python program in a file named `solution.py` that reads from standard input and writes the answer to standard output.

  Given a window size k and a list of integers, print the maximum of each contiguous window of size k as the window slides left to right. There are (len - k + 1) windows.

  Input: first integer k, then the list integers (len >= k >= 1).
  Output: the per-window maxima, space-separated.

  Examples (stdin -> stdout):
    '3\n1 3 -1 -3 5 3 6 7\n' -> '3 3 5 5 6 7'
    '1\n4 2 9\n' -> '4 2 9'
    '2\n1 2 3 4\n' -> '2 3 4'
    '4\n4 3 2 1\n' -> '4'
    '2\n5 5 5\n' -> '5 5'
    '3\n9 1 1 1 9\n' -> '9 1 9'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# python-med-10-window-max — Sliding window maximum

Given a window size k and a list of integers, print the maximum of each contiguous window of size k as the window slides left to right. There are (len - k + 1) windows.

- **Input:** first integer k, then the list integers (len >= k >= 1)
- **Output:** the per-window maxima, space-separated

## Examples

| stdin | stdout |
|---|---|
| `'3\n1 3 -1 -3 5 3 6 7\n'` | `'3 3 5 5 6 7'` |
| `'1\n4 2 9\n'` | `'4 2 9'` |
| `'2\n1 2 3 4\n'` | `'2 3 4'` |
| `'4\n4 3 2 1\n'` | `'4'` |
| `'2\n5 5 5\n'` | `'5 5'` |
| `'3\n9 1 1 1 9\n'` | `'9 1 9'` |
