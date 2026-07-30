---
id: c-easy-24-longest-word
tier: easy
source: coder_easy/longest-word
sandbox_path: solution.c
oracle: c-easy-24-longest-word-oracle.py
language: c
task: |
  Write a C program in a file named `solution.c` that reads from standard input and writes the answer to standard output.

  Read a line of whitespace-separated words and print the longest one. If several share the maximum length, print the first of them.

  Input: a line of words separated by spaces.
  Output: the longest word.

  Examples (stdin -> stdout):
    'the quick brown fox\n' -> 'quick'
    'a bb ccc\n' -> 'ccc'
    'hello\n' -> 'hello'
    'one two six\n' -> 'one'
    'ab cd ef\n' -> 'ab'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# c-easy-24-longest-word — Longest word

Read a line of whitespace-separated words and print the longest one. If several share the maximum length, print the first of them.

- **Input:** a line of words separated by spaces
- **Output:** the longest word

## Examples

| stdin | stdout |
|---|---|
| `'the quick brown fox\n'` | `'quick'` |
| `'a bb ccc\n'` | `'ccc'` |
| `'hello\n'` | `'hello'` |
| `'one two six\n'` | `'one'` |
| `'ab cd ef\n'` | `'ab'` |
