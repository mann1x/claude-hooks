---
id: rust-easy-29-median-odd
tier: easy
source: coder_easy/median-odd
sandbox_path: solution.rs
oracle: rust-easy-29-median-odd-oracle.py
language: rust
task: |
  Write a Rust program in a file named `solution.rs` that reads from standard input and writes the answer to standard output.

  Read an odd-length list of integers and print the median (the middle value of the sorted list).

  Input: whitespace-separated integers (an odd count).
  Output: a single integer: the median.

  Examples (stdin -> stdout):
    '3 1 2\n' -> '2'
    '5\n' -> '5'
    '9 1 5 3 7\n' -> '5'
    '-1 -3 -2\n' -> '-2'
    '10 20 30 40 50\n' -> '30'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# rust-easy-29-median-odd — Median (odd count)

Read an odd-length list of integers and print the median (the middle value of the sorted list).

- **Input:** whitespace-separated integers (an odd count)
- **Output:** a single integer: the median

## Examples

| stdin | stdout |
|---|---|
| `'3 1 2\n'` | `'2'` |
| `'5\n'` | `'5'` |
| `'9 1 5 3 7\n'` | `'5'` |
| `'-1 -3 -2\n'` | `'-2'` |
| `'10 20 30 40 50\n'` | `'30'` |
