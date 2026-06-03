---
id: go-easy-13-celsius-to-fahrenheit
tier: easy
source: coder_easy/celsius-to-fahrenheit
sandbox_path: solution.go
oracle: go-easy-13-celsius-to-fahrenheit-oracle.py
language: go
task: |
  Write a Go program in a file named `solution.go` that reads from standard input and writes the answer to standard output.

  Read an integer temperature in Celsius (a multiple of 5) and print it in Fahrenheit, computed as C*9/5+32.

  Input: a single integer Celsius value (a multiple of 5).
  Output: a single integer: the Fahrenheit value.

  Examples (stdin -> stdout):
    '0\n' -> '32'
    '100\n' -> '212'
    '-40\n' -> '-40'
    '5\n' -> '41'
    '-5\n' -> '23'
notes: |
  Auto-generated stdin/stdout easy problem (suite coder_easy).
  Uniform contract across all six languages.
---

# go-easy-13-celsius-to-fahrenheit — Celsius to Fahrenheit

Read an integer temperature in Celsius (a multiple of 5) and print it in Fahrenheit, computed as C*9/5+32.

- **Input:** a single integer Celsius value (a multiple of 5)
- **Output:** a single integer: the Fahrenheit value

## Examples

| stdin | stdout |
|---|---|
| `'0\n'` | `'32'` |
| `'100\n'` | `'212'` |
| `'-40\n'` | `'-40'` |
| `'5\n'` | `'41'` |
| `'-5\n'` | `'23'` |
