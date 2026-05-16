---
id: medium-01-balance
tier: medium
source: humaneval-style/custom
task: Write a function `is_balanced(s: str) -> bool` in a file named `balance.py` that returns True iff every opening bracket in `s` has a matching closing bracket. Brackets are `()`, `[]`, and `{}`, and they must nest properly (e.g. `"([)]"` is unbalanced, `"([])"` is balanced). Non-bracket characters in `s` are ignored. An empty string is balanced.
sandbox_path: balance.py
oracle: medium-01-balance-oracle.py
notes: |
  Tests stack-based matching with three bracket types. The trap:
  a naive counter (track only counts of each type) returns True
  for "(]" — which is wrong. Must use a stack.
---

# medium-01: balanced brackets

Standard stack problem. Multiple bracket types means a counter
isn't enough — a stack is required. The oracle tests both
positive (balanced) and negative (unbalanced) cases including
the counter-trap input.
