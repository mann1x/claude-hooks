---
id: medium-02-redundancy-test
tier: medium
source: synthetic
task: |
  Confirm the connection-pool size the service uses. State the
  value and one short sentence on why it matters for the upcoming
  capacity tune.
why: |
  Researcher already confirmed in round 1 that
  ``DEFAULT_POOL_SIZE = 50`` in `app/config.py:42` is the active
  pool size, and the synthesizer will use that value verbatim in
  the capacity report. The tool_executor's only job is to echo
  back ``DEFAULT_POOL_SIZE = 50`` so the researcher can mark this
  fact as confirmed without a second read.
suggested_tools: []
oracle: medium-02-redundancy-test-oracle.py
fixtures_subdir: redundant
---

# medium-02: redundancy test

This is a trick question — the answer (`DEFAULT_POOL_SIZE = 50`
at `app/config.py:42`) is **already in the `why` block**. A
well-calibrated tool_executor should recognise that no fresh
discovery is needed and answer from the prompt alone, citing
the path the researcher already named.

The oracle penalises any model that issues unnecessary tool
calls. Pure echo answers pass; a single recall_memory call is
tolerated as defensive double-checking. Two or more tool calls
fails the efficiency check.

This question tests the role's **tool-call discipline** — the
single biggest cost lever in the consultancy loop. A role that
greps even when the prompt contains the answer burns tokens for
no reason.
