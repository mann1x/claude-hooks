---
id: python-med-08-top-word
tier: medium
source: coder_med/top-word
sandbox_path: solution.py
oracle: python-med-08-top-word-oracle.py
language: python
task: |
  Write a Python program in a file named `solution.py` that reads from standard input and writes the answer to standard output.

  Read whitespace-separated words and print the word that occurs most often. If several words tie for the highest count, print the lexicographically smallest of them.

  Input: words separated by whitespace / newlines (at least one).
  Output: the most frequent word (lexicographic tie-break).

  Examples (stdin -> stdout):
    'a b a c a\n' -> 'a'
    'the the dog the dog\n' -> 'the'
    'x y\n' -> 'x'
    'banana apple banana apple\n' -> 'apple'
    'one\n' -> 'one'
    'b a b a c\n' -> 'a'
    'zoo ant ant zoo\n' -> 'ant'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# python-med-08-top-word — Most frequent word

Read whitespace-separated words and print the word that occurs most often. If several words tie for the highest count, print the lexicographically smallest of them.

- **Input:** words separated by whitespace / newlines (at least one)
- **Output:** the most frequent word (lexicographic tie-break)

## Examples

| stdin | stdout |
|---|---|
| `'a b a c a\n'` | `'a'` |
| `'the the dog the dog\n'` | `'the'` |
| `'x y\n'` | `'x'` |
| `'banana apple banana apple\n'` | `'apple'` |
| `'one\n'` | `'one'` |
| `'b a b a c\n'` | `'a'` |
| `'zoo ant ant zoo\n'` | `'ant'` |
