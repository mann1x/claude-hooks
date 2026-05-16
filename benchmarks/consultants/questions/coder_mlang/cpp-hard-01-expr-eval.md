---
id: cpp-hard-01-expr-eval
tier: hard
source: cs-classic
task: "Recursive-descent evaluator for `expr := term (('+'|'-') term)*; term := factor (('*'|'/') factor)*; factor := NUMBER | '(' expr ')'`."
sandbox_path: solution.cpp
oracle: cpp-hard-01-expr-eval-oracle.py
---

# cpp-hard-01-expr-eval

Implement a **recursive-descent expression evaluator** for the
grammar:

```
expr   := term (('+' | '-') term)*
term   := factor (('*' | '/') factor)*
factor := NUMBER | '(' expr ')'
NUMBER := -?[0-9]+
```

## I/O contract

Stdin: one or more lines, each holding one expression. Output
the evaluated integer value of each expression on its own line.

Whitespace around operators / operands is **ignored** (`1+2` and
`1 + 2` are equivalent). Division is integer division
(`std::div` toward zero).

Example:

```
$ printf "1+2*3\n(4-1)*5\n10/3\n-5+8\n" | ./sol
7
15
3
3
```

## Required structure

The grammar is non-trivial — the program must use **recursive
descent** (one function per non-terminal), not a shunting-yard
or eval-eval string-replace hack. The oracle greps the source
for `parseExpr` / `parseTerm` / `parseFactor` names (or
underscored equivalents) — implementations that don't expose
these named functions fail the structure check.

`main` reads lines via `std::getline`, invokes the parser per
line, prints the result + newline.

## Error handling

On malformed input (unbalanced parens, unexpected character,
empty expression, division by zero), the program may either:

- Print a single line `error` for that input line, or
- Set a non-zero exit code at end.

The oracle only checks **happy-path** correctness; malformed
inputs aren't part of the test set.

## Constraints

- Single file `solution.cpp`. Compiled via
  `g++ -O2 -std=c++17 -Wall -o sol solution.cpp -lpthread`.
- Stdlib only — `<string>`, `<string_view>`, `<iostream>`,
  `<cctype>`.
- Operator precedence + left-associativity must match standard
  math (multiplication binds tighter than addition; both are
  left-associative). The grammar above already encodes this.
- Integer overflow is undefined behavior territory — values in
  the oracle stay within `int32` range so a careful
  implementation has no issue.

