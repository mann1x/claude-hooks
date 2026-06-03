---
id: rust-easy-17-second-largest
tier: easy
source: coder_easy/second-largest
sandbox_path: solution.rs
oracle: rust-easy-17-second-largest-oracle.py
language: rust
task: |
  Write a Rust program in a file named `solution.rs` that reads from standard input and writes the answer to standard output.

  Read a list of integers (at least two distinct values) and print the second-largest distinct value.

  Input: whitespace-separated integers.
  Output: a single integer: the second-largest distinct value.

  Examples (stdin -> stdout):
    '3 1 4 1 5\n' -> '4'
    '10 20 30\n' -> '20'
    '5 5 4\n' -> '4'
    '-1 -2 -3\n' -> '-2'
    '7 7 8 8\n' -> '7'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# rust-easy-17-second-largest — Second largest distinct

Read a list of integers (at least two distinct values) and print the second-largest distinct value.

- **Input:** whitespace-separated integers
- **Output:** a single integer: the second-largest distinct value

## Examples

| stdin | stdout |
|---|---|
| `'3 1 4 1 5\n'` | `'4'` |
| `'10 20 30\n'` | `'20'` |
| `'5 5 4\n'` | `'4'` |
| `'-1 -2 -3\n'` | `'-2'` |
| `'7 7 8 8\n'` | `'7'` |
