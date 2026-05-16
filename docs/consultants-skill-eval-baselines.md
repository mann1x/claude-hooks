# Consultancy Skill-Eval Baselines

The running record of every model × suite × date that has been
scored through the Consultancy Skill-Eval Protocol. This file is
**append-only by convention** — corrections go in a new line with a
note rather than overwriting history, so we can spot upstream drift
across time.

For methodology, decision rubric, and how to re-run a suite, see
[`consultants-skill-eval-protocol.md`](consultants-skill-eval-protocol.md).

For the manifest of each suite version, see the per-suite
`SUITE.md` (e.g.
[`benchmarks/consultants/questions/coder/SUITE.md`](../benchmarks/consultants/questions/coder/SUITE.md)).

---

## Coder

The coder suite gates `cfg.roles.coder.model` per the M10 role
infrastructure.

| Date       | Suite ver. | Model                          | Pass rate | Avg quality | Median tokens | Median wall | Suite hash | Notes |
|------------|-----------:|--------------------------------|----------:|------------:|--------------:|------------:|------------|-------|
| _no live runs yet — first run lands in a follow-up commit_ |

### Recommended default

_Not yet established. M10 ships the role disabled-by-default; the
project-global `DEFAULT_MODEL` (`kimi-k2.6:cloud`) applies until
the first live run produces a rubric-qualifying candidate._

---

## Stall thresholds

The stall suite (M11a, not yet shipped) will gate per-model
`stall_threshold_s` and `hard_cap_s` defaults in
`consultants/engine/stall_defaults.py`.

| Date | Suite ver. | Model | inter-token p99 | recommended stall_s | recommended hard_cap_s | Suite hash | Notes |
|------|-----------:|-------|-----------------|---------------------|------------------------|------------|-------|
| _M11a not yet shipped_ |

---

## Tool executor

The tool_executor suite (M11c, not yet shipped) will gate
`cfg.roles.tool_executor.model` + the role's default-on bit.

| Date | Suite ver. | Configuration                                | Tool-call success | Wasted calls | Median wall | Final-answer score | Suite hash | Notes |
|------|-----------:|----------------------------------------------|-------------------|--------------|-------------|--------------------|------------|-------|
| _M11c not yet shipped_ |

---

## Adding a row

After a live run produces a report with a recommended default
(or a clear non-qualifying score worth recording):

1. Read the report at
   `benchmarks/consultants/results/<date>/<suite>/report.md`.
2. Find the per-model summary table; copy the row(s) you want to
   record.
3. Add one row to the appropriate table above, in date order
   (newest at the bottom so reading top-to-bottom is chronological).
4. The `Suite hash` column is the **first 8 characters** of the
   manifest's `suite_hash` — enough to disambiguate while staying
   readable.
5. The `Notes` column is free-form: cloud-flap-during-run,
   manually-rejected-rubric-winner, etc.

If you re-score a model whose row already exists, **append a new
row** with the new date rather than editing the old one. A single
model with two rows on the same suite version is fine and informative
(it shows variance / cloud drift).
