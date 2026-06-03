---
id: go-easy-08-fizzbuzz
tier: easy
source: coder_easy/fizzbuzz
sandbox_path: solution.go
oracle: go-easy-08-fizzbuzz-oracle.py
language: go
task: |
  Write a Go program in a file named `solution.go` that reads from standard input and writes the answer to standard output.

  Read an integer n and print the lines 1..n. For multiples of 3 print "Fizz", for multiples of 5 print "Buzz", for multiples of both print "FizzBuzz", otherwise print the number. One value per line.

  Input: a single integer n (n ≥ 1).
  Output: n lines, the FizzBuzz value for 1..n.

  Examples (stdin -> stdout):
    '5\n' -> '1\n2\nFizz\n4\nBuzz'
    '3\n' -> '1\n2\nFizz'
    '1\n' -> '1'
    '2\n' -> '1\n2'
    '15\n' -> '1\n2\nFizz\n4\nBuzz\nFizz\n7\n8\nFizz\nBuzz\n11\nFizz\n13\n14\nFizzBuzz'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# go-easy-08-fizzbuzz — FizzBuzz

Read an integer n and print the lines 1..n. For multiples of 3 print "Fizz", for multiples of 5 print "Buzz", for multiples of both print "FizzBuzz", otherwise print the number. One value per line.

- **Input:** a single integer n (n ≥ 1)
- **Output:** n lines, the FizzBuzz value for 1..n

## Examples

| stdin | stdout |
|---|---|
| `'5\n'` | `'1\n2\nFizz\n4\nBuzz'` |
| `'3\n'` | `'1\n2\nFizz'` |
| `'1\n'` | `'1'` |
| `'2\n'` | `'1\n2'` |
| `'15\n'` | `'1\n2\nFizz\n4\nBuzz\nFizz\n7\n8\nFizz\nBuzz\n11\nFizz\n13\n14\nFizzBuzz'` |
