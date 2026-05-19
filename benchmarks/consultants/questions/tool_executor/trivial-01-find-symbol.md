---
id: trivial-01-find-symbol
tier: trivial
source: synthetic
task: |
  Find where the constant `DEFAULT_TIMEOUT_S` is defined in the
  codebase. Return the file path, the line number, and the
  constant's value. Cite the location as `path:line`.
why: "Researcher needs to confirm the current default timeout before recommending a tune"
suggested_tools:
  - grep
  - read_file
oracle: trivial-01-find-symbol-oracle.py
fixtures_subdir: simple_constants
---

# trivial-01: find a single constant definition

The simplest possible tool_executor task — one fixture file, one
constant, one citation. A model that can't grep + cite is not
viable for the role; the oracle catches both content (the value
`30.0`) and citation (`config.py:18`, the line where the constant
is declared).
