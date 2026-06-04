---
id: csharp-med-06-spiral-order
tier: medium
source: coder_med/spiral-order
sandbox_path: solution.cs
oracle: csharp-med-06-spiral-order-oracle.py
language: csharp
task: |
  Write a C# program in a file named `solution.cs` that reads from standard input and writes the answer to standard output.

  Read a matrix and print its elements in clockwise spiral order starting from the top-left, going right, then down, then left, then up, spiralling inward.

  Input: first two integers R and C, then R*C integers row-major.
  Output: the R*C values in spiral order, space-separated.

  Examples (stdin -> stdout):
    '3 3\n1 2 3 4 5 6 7 8 9\n' -> '1 2 3 6 9 8 7 4 5'
    '1 4\n1 2 3 4\n' -> '1 2 3 4'
    '4 1\n1 2 3 4\n' -> '1 2 3 4'
    '2 2\n1 2 3 4\n' -> '1 2 4 3'
    '2 3\n1 2 3 4 5 6\n' -> '1 2 3 6 5 4'
    '3 2\n1 2 3 4 5 6\n' -> '1 2 4 6 5 3'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# csharp-med-06-spiral-order — Spiral matrix traversal

Read a matrix and print its elements in clockwise spiral order starting from the top-left, going right, then down, then left, then up, spiralling inward.

- **Input:** first two integers R and C, then R*C integers row-major
- **Output:** the R*C values in spiral order, space-separated

## Examples

| stdin | stdout |
|---|---|
| `'3 3\n1 2 3 4 5 6 7 8 9\n'` | `'1 2 3 6 9 8 7 4 5'` |
| `'1 4\n1 2 3 4\n'` | `'1 2 3 4'` |
| `'4 1\n1 2 3 4\n'` | `'1 2 3 4'` |
| `'2 2\n1 2 3 4\n'` | `'1 2 4 3'` |
| `'2 3\n1 2 3 4 5 6\n'` | `'1 2 3 6 5 4'` |
| `'3 2\n1 2 3 4 5 6\n'` | `'1 2 4 6 5 3'` |
