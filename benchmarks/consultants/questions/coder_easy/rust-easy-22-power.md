---
id: rust-easy-22-power
tier: easy
source: coder_easy/power
sandbox_path: solution.rs
oracle: rust-easy-22-power-oracle.py
language: rust
task: |
  Write a Rust program in a file named `solution.rs` that reads from standard input and writes the answer to standard output.

  Read two non-negative integers, base and exponent, and print base raised to that exponent.

  Input: two whitespace-separated integers: base and exponent (exponent ≥ 0).
  Output: a single integer: base ** exponent.

  Examples (stdin -> stdout):
    '2 10\n' -> '1024'
    '3 0\n' -> '1'
    '5 3\n' -> '125'
    '10 2\n' -> '100'
    '2 0\n' -> '1'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# rust-easy-22-power — Integer power

Read two non-negative integers, base and exponent, and print base raised to that exponent.

- **Input:** two whitespace-separated integers: base and exponent (exponent ≥ 0)
- **Output:** a single integer: base ** exponent

## Examples

| stdin | stdout |
|---|---|
| `'2 10\n'` | `'1024'` |
| `'3 0\n'` | `'1'` |
| `'5 3\n'` | `'125'` |
| `'10 2\n'` | `'100'` |
| `'2 0\n'` | `'1'` |
