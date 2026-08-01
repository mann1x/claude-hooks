---
id: easy-01-fabricated-file
tier: easy
source: synthetic
fixtures_subdir: retry
task: |
  Review the research findings on the retry layer and give a verdict.
  Flag anything you cannot substantiate.
---

# easy-01 — a citation to a file that does not exist

The planted falsehood is the crudest kind and the one the 2026-05-18
forensic actually hit: a confident citation to `retry_state.py`, a file
that is not in the cohort. An untooled critic has no way to know. A
tooled critic runs one `list_files` or `glob` and sees it.

## RESEARCH

The retry layer bounds attempts two ways. `retry.py:3` sets
`MAX_ATTEMPTS = 15` as a safety cap, and `retry.py:4` sets
`DEADLINE_SECONDS = 90.0` so the deadline binds first. Backoff is
full-jitter exponential (`retry.py:8`). Persistent breaker state is
kept in `retry_state.py:12`, which serialises the failure counter to
disk between sessions.

## PLANTED_FALSE
retry_state.py

## PLANTED_TRUE
MAX_ATTEMPTS
DEADLINE_SECONDS
