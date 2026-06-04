---
id: csharp-med-03-roman-to-int
tier: medium
source: coder_med/roman-to-int
sandbox_path: solution.cs
oracle: csharp-med-03-roman-to-int-oracle.py
language: csharp
task: |
  Write a C# program in a file named `solution.cs` that reads from standard input and writes the answer to standard output.

  Convert an uppercase Roman numeral to its integer value. Use the subtractive rule: a smaller symbol immediately before a larger one is subtracted (IV=4, IX=9, XL=40, XC=90, CD=400, CM=900).

  Input: one line: a valid Roman numeral (I V X L C D M).
  Output: the integer value.

  Examples (stdin -> stdout):
    'III\n' -> '3'
    'IV\n' -> '4'
    'IX\n' -> '9'
    'LVIII\n' -> '58'
    'MCMXCIV\n' -> '1994'
    'XL\n' -> '40'
    'MMXXIV\n' -> '2024'
notes: |
  Auto-generated stdin/stdout easy-medium problem (suite coder_med).
  Uniform contract across all six languages.
---

# csharp-med-03-roman-to-int — Roman numeral to integer

Convert an uppercase Roman numeral to its integer value. Use the subtractive rule: a smaller symbol immediately before a larger one is subtracted (IV=4, IX=9, XL=40, XC=90, CD=400, CM=900).

- **Input:** one line: a valid Roman numeral (I V X L C D M)
- **Output:** the integer value

## Examples

| stdin | stdout |
|---|---|
| `'III\n'` | `'3'` |
| `'IV\n'` | `'4'` |
| `'IX\n'` | `'9'` |
| `'LVIII\n'` | `'58'` |
| `'MCMXCIV\n'` | `'1994'` |
| `'XL\n'` | `'40'` |
| `'MMXXIV\n'` | `'2024'` |
