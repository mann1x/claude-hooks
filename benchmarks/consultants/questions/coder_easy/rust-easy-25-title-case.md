---
id: rust-easy-25-title-case
tier: easy
source: coder_easy/title-case
sandbox_path: solution.rs
oracle: rust-easy-25-title-case-oracle.py
language: rust
task: |
  Write a Rust program in a file named `solution.rs` that reads from standard input and writes the answer to standard output.

  Read a line of words and print each word with its first letter uppercased and the remaining letters lowercased, separated by single spaces.

  Input: a line of words separated by spaces.
  Output: the words in title case, single-spaced.

  Examples (stdin -> stdout):
    'hello world\n' -> 'Hello World'
    'the QUICK fox\n' -> 'The Quick Fox'
    'abc def\n' -> 'Abc Def'
    'HELLO\n' -> 'Hello'
    'mixED cASE\n' -> 'Mixed Case'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# rust-easy-25-title-case — Title case

Read a line of words and print each word with its first letter uppercased and the remaining letters lowercased, separated by single spaces.

- **Input:** a line of words separated by spaces
- **Output:** the words in title case, single-spaced

## Examples

| stdin | stdout |
|---|---|
| `'hello world\n'` | `'Hello World'` |
| `'the QUICK fox\n'` | `'The Quick Fox'` |
| `'abc def\n'` | `'Abc Def'` |
| `'HELLO\n'` | `'Hello'` |
| `'mixED cASE\n'` | `'Mixed Case'` |
