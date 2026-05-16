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
| 2026-05-16 |        1.0 | `glm-5.1:cloud`                |   100%    |    4.88     |      1841     |      4.9s   | `9aa6eaf0` | **rubric winner** — fastest qualifying model with best quality |
| 2026-05-16 |        1.0 | `kimi-k2.6:cloud`              |   100%    |    4.86     |      1957     |     10.0s   | `9aa6eaf0` | runner-up, ~tied on quality with glm but ~2× wall; **note: 1/8 trials had judge-empty-response → score computed from 7 trials** (harness hardened in same commit) |
| 2026-05-16 |        1.0 | `gemma4:31b-cloud`             |   100%    |    4.62     |      1876     |     14.5s   | `9aa6eaf0` | slowest tail (peak 105 s on hard-01); only sub-4 cell (3.0 on hard-02-digit-filter) |
| 2026-05-16 |        1.0 | `qwen3-coder-next:cloud`       |   100%    |    4.50     |      2111     |      2.9s   | `9aa6eaf0` | fastest median wall but most verbose / lowest avg quality. Pre-run wrong-name guess `qwen3-next:cloud` → corrected to `qwen3-coder-next:cloud` (the coder variant) |

### Recommended default

**`glm-5.1:cloud`** as of 2026-05-16, baked into
[`consultants/engine/coder_defaults.py`](../consultants/engine/coder_defaults.py).
Wins the v1.0 rubric on `avg_quality` (4.88) and the tokens
tie-breaker (1841 median); ~2× faster median wall than the
runner-up. All 4 candidates qualified at `pass_rate ≥ 70%` AND
`avg_quality ≥ 3.5`, so this is a "pick the best of qualifying"
decision, not "the only one that worked".

Bench commit: `554a354` (signature fix that unblocked live runs).
Live-run commit: see the M11b live-run commit that appended this
row. Results dir:
[`benchmarks/consultants/results/2026-05-16/coder/`](../benchmarks/consultants/results/2026-05-16/coder/).

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
