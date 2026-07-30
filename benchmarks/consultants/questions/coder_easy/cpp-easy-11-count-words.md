---
id: cpp-easy-11-count-words
tier: easy
source: coder_easy/count-words
sandbox_path: solution.cpp
oracle: cpp-easy-11-count-words-oracle.py
language: cpp
task: |
  Write a C++ program in a file named `solution.cpp` that reads from standard input and writes the answer to standard output.

  Read text and print the number of whitespace-separated words.

  Input: a line of text (words separated by spaces).
  Output: a single integer: the word count.

  Examples (stdin -> stdout):
    'the quick brown fox\n' -> '4'
    'hello\n' -> '1'
    '  a  b  \n' -> '2'
    'one two three\n' -> '3'
    '\n' -> '0'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# cpp-easy-11-count-words — Count words

Read text and print the number of whitespace-separated words.

- **Input:** a line of text (words separated by spaces)
- **Output:** a single integer: the word count

## Examples

| stdin | stdout |
|---|---|
| `'the quick brown fox\n'` | `'4'` |
| `'hello\n'` | `'1'` |
| `'  a  b  \n'` | `'2'` |
| `'one two three\n'` | `'3'` |
| `'\n'` | `'0'` |
