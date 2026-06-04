---
id: rust-med-07-max-subarray
tier: medium
source: coder_med/max-subarray
sandbox_path: solution.rs
oracle: rust-med-07-max-subarray-oracle.py
language: rust
task: |
  Write a Rust program in a file named `solution.rs` that reads from standard input and writes the answer to standard output.

  Print the maximum sum of any non-empty contiguous subarray (Kadane's algorithm). The array may be entirely negative, in which case the answer is the largest single element.

  Input: one line of whitespace-separated integers (at least one).
  Output: the maximum contiguous subarray sum.

  Examples (stdin -> stdout):
    '-2 1 -3 4 -1 2 1 -5 4\n' -> '6'
    '1 2 3 4\n' -> '10'
    '-1 -2 -3\n' -> '-1'
    '5\n' -> '5'
    '-5\n' -> '-5'
    '3 -2 5 -1\n' -> '6'
    '-2 -1\n' -> '-1'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# rust-med-07-max-subarray — Maximum subarray sum

Print the maximum sum of any non-empty contiguous subarray (Kadane's algorithm). The array may be entirely negative, in which case the answer is the largest single element.

- **Input:** one line of whitespace-separated integers (at least one)
- **Output:** the maximum contiguous subarray sum

## Examples

| stdin | stdout |
|---|---|
| `'-2 1 -3 4 -1 2 1 -5 4\n'` | `'6'` |
| `'1 2 3 4\n'` | `'10'` |
| `'-1 -2 -3\n'` | `'-1'` |
| `'5\n'` | `'5'` |
| `'-5\n'` | `'-5'` |
| `'3 -2 5 -1\n'` | `'6'` |
| `'-2 -1\n'` | `'-1'` |
