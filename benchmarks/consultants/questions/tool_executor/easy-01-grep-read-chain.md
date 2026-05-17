---
id: easy-01-grep-read-chain
tier: easy
source: synthetic
task: |
  Find the function `handle_auth` in this codebase and return its
  full body verbatim. Include the file path and the line range
  where the function lives (cite as `path:start-end`).
why: "Researcher needs to audit the auth flow before reporting"
suggested_tools:
  - grep
  - read_file
oracle: easy-01-grep-read-chain-oracle.py
fixtures_subdir: auth
---

# easy-01: grep + read a function body

Two-file cohort with one red-herring file (`db.py`). The model
must grep for `handle_auth` to find `auth.py`, read it, and
return the function body — including the docstring and at least
one identifying line of code (the `record = store.get(username)`
lookup). The oracle penalises pure recall_memory answers (no
tool calls) and answers that omit either the file path or the
function signature.
