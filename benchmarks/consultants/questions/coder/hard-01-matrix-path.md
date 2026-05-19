---
id: hard-01-matrix-path
tier: hard
source: humaneval-style/custom (classic-DP)
task: Write a function `min_path_sum(grid: list[list[int]]) -> int` in a file named `min_path.py` that returns the minimum sum of any path from the top-left cell `grid[0][0]` to the bottom-right cell `grid[m-1][n-1]`, moving only RIGHT or DOWN one cell at a time. `grid` is a non-empty rectangular matrix of integers (any sign). Raise `ValueError` when the grid is empty, when any row is empty, or when the grid is non-rectangular.
sandbox_path: min_path.py
oracle: hard-01-matrix-path-oracle.py
notes: |
  Classic DP problem. Traps:
  - Naive recursion is exponential without memoization; the oracle
    runs a 10x10 grid with a 3s timeout to catch it.
  - Single-cell grid (1x1) should return that cell's value.
  - Negative numbers are allowed; "minimum" still means the actual
    smallest sum.
  - Input validation: empty / jagged grids.
---

# hard-01: minimum path sum

Classic 2-D dynamic-programming problem. Tests:
1. Correctness on standard cases.
2. Edge cases (1x1, 1xN row, Mx1 column).
3. Negative values (sum can be negative).
4. Performance via timeout on a 10×10 grid.
5. Input validation (empty / jagged).
