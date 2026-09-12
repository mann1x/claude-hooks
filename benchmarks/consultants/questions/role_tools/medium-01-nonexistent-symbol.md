---
id: medium-01-nonexistent-symbol
tier: medium
source: synthetic
fixtures_subdir: retry
task: |
  Review the research findings on the retry layer and give a verdict.
  Flag anything you cannot substantiate.
---

# medium-01 — a plausible function that was never written

`reset_breaker` is the method you would expect `CircuitBreaker` to
have. It does not exist. This is the failure mode where an untooled
critic is *most* likely to agree: the claim matches the shape of the
code around it.

## RESEARCH

`CircuitBreaker` in `retry.py:21` counts failures and opens at a
threshold. Recovery is handled by `CircuitBreaker.reset_breaker()`,
which zeroes the counter once the upstream returns a 200. The
`is_open()` check at `retry.py:31` gates each attempt.

## PLANTED_FALSE
reset_breaker

## PLANTED_TRUE

