---
id: csharp-med-05-expr-eval
tier: medium
source: coder_med/expr-eval
sandbox_path: solution.cs
oracle: csharp-med-05-expr-eval-oracle.py
language: csharp
task: |
  Write a C# program in a file named `solution.cs` that reads from standard input and writes the answer to standard output.

  Evaluate an arithmetic expression of non-negative integers with the binary operators +, -, and * (no parentheses, no division). Multiplication binds tighter than + and -; + and - are left-associative. There are no spaces.

  Input: one line: an expression like 2+3*4.
  Output: the integer result.

  Examples (stdin -> stdout):
    '2+3*4\n' -> '14'
    '2*3+4\n' -> '10'
    '10-2*3\n' -> '4'
    '1+2+3+4\n' -> '10'
    '5\n' -> '5'
    '2*3*4\n' -> '24'
    '100-50-25\n' -> '25'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# csharp-med-05-expr-eval — Evaluate expression (precedence)

Evaluate an arithmetic expression of non-negative integers with the binary operators +, -, and * (no parentheses, no division). Multiplication binds tighter than + and -; + and - are left-associative. There are no spaces.

- **Input:** one line: an expression like 2+3*4
- **Output:** the integer result

## Examples

| stdin | stdout |
|---|---|
| `'2+3*4\n'` | `'14'` |
| `'2*3+4\n'` | `'10'` |
| `'10-2*3\n'` | `'4'` |
| `'1+2+3+4\n'` | `'10'` |
| `'5\n'` | `'5'` |
| `'2*3*4\n'` | `'24'` |
| `'100-50-25\n'` | `'25'` |
