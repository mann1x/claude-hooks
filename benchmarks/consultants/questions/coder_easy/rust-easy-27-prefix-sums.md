---
id: rust-easy-27-prefix-sums
tier: easy
source: coder_easy/prefix-sums
sandbox_path: solution.rs
oracle: rust-easy-27-prefix-sums-oracle.py
language: rust
task: |
  Write a Rust program in a file named `solution.rs` that reads from standard input and writes the answer to standard output.

  Read a list of integers and print their running (prefix) sums, separated by single spaces. The i-th output is the sum of the first i inputs.

  Input: whitespace-separated integers.
  Output: the prefix sums, space-separated.

  Examples (stdin -> stdout):
    '1 2 3\n' -> '1 3 6'
    '5\n' -> '5'
    '1 1 1 1\n' -> '1 2 3 4'
    '-1 1 -1\n' -> '-1 0 -1'
    '10 20\n' -> '10 30'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# rust-easy-27-prefix-sums — Prefix sums

Read a list of integers and print their running (prefix) sums, separated by single spaces. The i-th output is the sum of the first i inputs.

- **Input:** whitespace-separated integers
- **Output:** the prefix sums, space-separated

## Examples

| stdin | stdout |
|---|---|
| `'1 2 3\n'` | `'1 3 6'` |
| `'5\n'` | `'5'` |
| `'1 1 1 1\n'` | `'1 2 3 4'` |
| `'-1 1 -1\n'` | `'-1 0 -1'` |
| `'10 20\n'` | `'10 30'` |
