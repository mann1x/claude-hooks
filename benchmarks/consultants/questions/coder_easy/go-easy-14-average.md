---
id: go-easy-14-average
tier: easy
source: coder_easy/average
sandbox_path: solution.go
oracle: go-easy-14-average-oracle.py
language: go
task: |
  Write a Go program in a file named `solution.go` that reads from standard input and writes the answer to standard output.

  Read a list of integers whose sum divides evenly by their count, and print their integer average (sum divided by count).

  Input: whitespace-separated integers.
  Output: a single integer: the average.

  Examples (stdin -> stdout):
    '2 4 6\n' -> '4'
    '10 20\n' -> '15'
    '5 5 5\n' -> '5'
    '1 2 3 4 5\n' -> '3'
    '100\n' -> '100'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# go-easy-14-average — Integer average

Read a list of integers whose sum divides evenly by their count, and print their integer average (sum divided by count).

- **Input:** whitespace-separated integers
- **Output:** a single integer: the average

## Examples

| stdin | stdout |
|---|---|
| `'2 4 6\n'` | `'4'` |
| `'10 20\n'` | `'15'` |
| `'5 5 5\n'` | `'5'` |
| `'1 2 3 4 5\n'` | `'3'` |
| `'100\n'` | `'100'` |
