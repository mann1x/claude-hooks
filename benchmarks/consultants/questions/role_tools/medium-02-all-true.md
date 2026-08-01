---
id: medium-02-all-true
tier: medium
source: synthetic
fixtures_subdir: retry
task: |
  Review the research findings on the retry layer and give a verdict.
  Flag anything you cannot substantiate.
---

# medium-02 — the control: nothing is wrong

The precision case, and the reason recall alone is not a score. A
tooled critic that flags something here is crying wolf, and a council
whose critic cries wolf is worse than one that stays quiet. Every
claim below is verifiable in the cohort.

## RESEARCH

`forwarder.py:9` loops attempts 1..15. Each iteration consults
`should_retry` (`retry.py:14`), which returns False once `elapsed`
reaches `DEADLINE_SECONDS`. Retryable statuses are 429, 500, 502, 503
and 529 (`retry.py:18`). `KEEPALIVE_SECONDS = 15` sits at
`forwarder.py:5`.

## PLANTED_FALSE

## PLANTED_TRUE
should_retry
KEEPALIVE_SECONDS
529
