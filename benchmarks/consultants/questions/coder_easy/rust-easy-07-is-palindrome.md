---
id: rust-easy-07-is-palindrome
tier: easy
source: coder_easy/is-palindrome
sandbox_path: solution.rs
oracle: rust-easy-07-is-palindrome-oracle.py
language: rust
task: |
  Write a Rust program in a file named `solution.rs` that reads from standard input and writes the answer to standard output.

  Read one line and print "yes" if it reads the same forwards and backwards, otherwise "no". Comparison is exact (case-sensitive).

  Input: a single line of text.
  Output: "yes" or "no".

  Examples (stdin -> stdout):
    'racecar\n' -> 'yes'
    'hello\n' -> 'no'
    'abba\n' -> 'yes'
    'a\n' -> 'yes'
    'ab\n' -> 'no'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# rust-easy-07-is-palindrome — Palindrome check

Read one line and print "yes" if it reads the same forwards and backwards, otherwise "no". Comparison is exact (case-sensitive).

- **Input:** a single line of text
- **Output:** "yes" or "no"

## Examples

| stdin | stdout |
|---|---|
| `'racecar\n'` | `'yes'` |
| `'hello\n'` | `'no'` |
| `'abba\n'` | `'yes'` |
| `'a\n'` | `'yes'` |
| `'ab\n'` | `'no'` |
