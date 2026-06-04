---
id: go-med-04-merge-intervals
tier: medium
source: coder_med/merge-intervals
sandbox_path: solution.go
oracle: go-med-04-merge-intervals-oracle.py
language: go
task: |
  Write a Go program in a file named `solution.go` that reads from standard input and writes the answer to standard output.

  Merge overlapping closed integer intervals. The input is a count N followed by 2*N integers giving N intervals as (start, end) pairs. Merge intervals that overlap or touch (end == next start counts as overlap) and print the merged intervals sorted by start, as a flat space-separated list of start end start end ...

  Input: N then 2*N integers (whitespace-separated, any layout).
  Output: the merged intervals flattened: s1 e1 s2 e2 ....

  Examples (stdin -> stdout):
    '3\n1 3 2 6 8 10\n' -> '1 6 8 10'
    '1\n5 7\n' -> '5 7'
    '2\n1 4 4 5\n' -> '1 5'
    '3\n1 2 3 4 5 6\n' -> '1 2 3 4 5 6'
    '2\n1 10 2 3\n' -> '1 10'
    '4 1 3 0 0 9 12 2 4\n' -> '0 0 1 4 9 12'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# go-med-04-merge-intervals — Merge intervals

Merge overlapping closed integer intervals. The input is a count N followed by 2*N integers giving N intervals as (start, end) pairs. Merge intervals that overlap or touch (end == next start counts as overlap) and print the merged intervals sorted by start, as a flat space-separated list of start end start end ...

- **Input:** N then 2*N integers (whitespace-separated, any layout)
- **Output:** the merged intervals flattened: s1 e1 s2 e2 ...

## Examples

| stdin | stdout |
|---|---|
| `'3\n1 3 2 6 8 10\n'` | `'1 6 8 10'` |
| `'1\n5 7\n'` | `'5 7'` |
| `'2\n1 4 4 5\n'` | `'1 5'` |
| `'3\n1 2 3 4 5 6\n'` | `'1 2 3 4 5 6'` |
| `'2\n1 10 2 3\n'` | `'1 10'` |
| `'4 1 3 0 0 9 12 2 4\n'` | `'0 0 1 4 9 12'` |
