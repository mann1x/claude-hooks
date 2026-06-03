---
id: go-easy-28-is-anagram
tier: easy
source: coder_easy/is-anagram
sandbox_path: solution.go
oracle: go-easy-28-is-anagram-oracle.py
language: go
task: |
  Write a Go program in a file named `solution.go` that reads from standard input and writes the answer to standard output.

  Read two lines and print "yes" if the second is an anagram of the first (same multiset of characters), otherwise "no". Comparison is exact and case-sensitive.

  Input: two lines: the first string, then the second string.
  Output: "yes" or "no".

  Examples (stdin -> stdout):
    'listen\nsilent\n' -> 'yes'
    'abc\ncba\n' -> 'yes'
    'abc\nabd\n' -> 'no'
    'aabb\nbbaa\n' -> 'yes'
    'a\nb\n' -> 'no'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# go-easy-28-is-anagram — Anagram check

Read two lines and print "yes" if the second is an anagram of the first (same multiset of characters), otherwise "no". Comparison is exact and case-sensitive.

- **Input:** two lines: the first string, then the second string
- **Output:** "yes" or "no"

## Examples

| stdin | stdout |
|---|---|
| `'listen\nsilent\n'` | `'yes'` |
| `'abc\ncba\n'` | `'yes'` |
| `'abc\nabd\n'` | `'no'` |
| `'aabb\nbbaa\n'` | `'yes'` |
| `'a\nb\n'` | `'no'` |
