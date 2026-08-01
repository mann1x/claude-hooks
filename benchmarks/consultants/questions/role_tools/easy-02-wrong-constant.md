---
id: easy-02-wrong-constant
tier: easy
source: synthetic
fixtures_subdir: retry
task: |
  Review the research findings on the retry layer and give a verdict.
  Flag anything you cannot substantiate.
---

# easy-02 — a real file, a wrong value

Subtler than a fabricated path: the file and symbol exist, the number
is wrong. `read_file` settles it; reasoning alone cannot, because the
claim is internally plausible.

## RESEARCH

The forwarder retries up to `MAX_ATTEMPTS` attempts. `retry.py:3`
defines `MAX_ATTEMPTS = 5`, so a sustained upstream brownout is
abandoned quickly. The wall-clock deadline in `retry.py:4` is
`DEADLINE_SECONDS = 90.0`.

## PLANTED_FALSE
MAX_ATTEMPTS = 5

## PLANTED_TRUE
DEADLINE_SECONDS = 90.0
