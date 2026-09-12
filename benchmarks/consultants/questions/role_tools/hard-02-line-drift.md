---
id: hard-02-line-drift
tier: hard
source: synthetic
fixtures_subdir: retry
task: |
  Review the research findings on the retry layer and give a verdict.
  Flag anything you cannot substantiate.
---

# hard-02 — right symbol, wrong line

The hardest planted error, and deliberately near the limit of what a
keyword oracle can score. `should_retry` is real; the cited line points
at the module docstring. Catching it requires actually reading the
file, not just confirming the symbol exists — the distinction between
`grep` and `read_file` as verification.

## RESEARCH

Retry eligibility is decided by `should_retry` at `retry.py:1`, which
also enforces the deadline. `MAX_ATTEMPTS = 15` at `retry.py:3` is the
safety cap.

## PLANTED_FALSE
retry.py:1

## PLANTED_TRUE
MAX_ATTEMPTS

## CORRECTION_TOKENS
retry.py:14
