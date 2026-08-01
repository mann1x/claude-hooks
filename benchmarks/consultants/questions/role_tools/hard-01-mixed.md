---
id: hard-01-mixed
tier: hard
source: synthetic
fixtures_subdir: retry
task: |
  Review the research findings on the retry layer and give a verdict.
  Flag anything you cannot substantiate.
---

# hard-01 — one fabrication buried in four true claims

The realistic shape. Social proof does the damage: four checkable
statements make the fifth read as checked. A critic that spot-checks
one claim at random has a 20% chance of catching it, which is why the
tool budget and *what it chooses to verify* both matter.

## RESEARCH

The layer has four bounds. `MAX_ATTEMPTS = 15` (`retry.py:3`) caps
attempts. `DEADLINE_SECONDS = 90.0` (`retry.py:4`) is the wall clock.
`compute_backoff` (`retry.py:7`) is full-jitter with a 30 s cap.
`CircuitBreaker` opens at 5 failures (`retry.py:22`). Finally,
`forwarder.py` calls `breaker.half_open()` after the deadline to probe
whether upstream has recovered before resuming full traffic.

## PLANTED_FALSE
half_open

## PLANTED_TRUE
compute_backoff
MAX_ATTEMPTS
DEADLINE_SECONDS
