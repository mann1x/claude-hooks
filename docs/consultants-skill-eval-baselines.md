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

### v1.0.1-mlang baseline (2026-05-17) — per-language winners

The mlang v1.0.1 delta fixed the `pytest -x` algorithm-axis bug
(see commit `bench(coder_mlang): v1.0.1 two-axis oracle scoring`)
and re-ran the same 5-model cohort across **6 languages × 13
questions**. The results revealed that no single model is best
across all languages — the per-language map below replaces the
v1.0 "one model wins all" baseline as task #111's default. Suite
hash `ddef8095`.

| Language | Primary             | Fallback                 | alg% | avgQ | Source                                |
|----------|---------------------|--------------------------|-----:|-----:|---------------------------------------|
| c        | `glm-5.1:cloud`     | `deepseek-v4-pro:cloud`  |  50% | 2.83 | user "pick fastest" (table ambiguous) |
| cpp      | `deepseek-v4-flash:cloud` | `kimi-k2.6:cloud`  |  33% | 4.33 | table alg + quality winner            |
| csharp   | `deepseek-v4-pro:cloud`   | `kimi-k2.6:cloud`  |  33% | 3.67 | user override (top avgQ)              |
| go       | `kimi-k2.6:cloud`         | `deepseek-v4-pro:cloud` | 0% | 4.33 | table avgQ winner                     |
| python   | `glm-5.1:cloud`           | `kimi-k2.6:cloud`  |  0%  | 4.67 | user override (top avgQ tied)         |
| rust     | `deepseek-v4-flash:cloud` | `deepseek-v4-pro:cloud` | 0% | 2.83 | table avgQ winner                     |

**Global default route** (used when a language has no per-language
entry, including all out-of-cohort languages like `typescript`,
`java`, `ruby`, `swift`, `shell`):
`glm-5.1:cloud` → `kimi-k2.6:cloud`.

Constants live in
[`consultants/engine/coder_defaults.py`](../consultants/engine/coder_defaults.py)
as `RECOMMENDED_CODER_ROUTES_BY_LANGUAGE` +
`RECOMMENDED_CODER_DEFAULT_ROUTE`. Results dir:
[`benchmarks/consultants/results/2026-05-17/coder_mlang-v1.0.1/`](../benchmarks/consultants/results/2026-05-17/coder_mlang-v1.0.1/)
(merged with `rerun-20260517-114651/` for the 1 timeout-tagged
trial that re-fired clean).

The `fallback` column is the cohort-wide #2-by-avgQ when not
already the primary — so a primary failure lands on a model that
was still strong-for-that-language rather than a random survivor.
Failover triggers (in `coder.py`) are: any exception, zero files
written, OR empty final assistant message — strictest of the
three wins for the recorded reason.

Override surface (task #111):
- TOML: `[role.coder.routes.<lang>]` + `[role.coder.default_route]`
  (see `consultants/config.py:_render`).
- CLI: `claude-consultants config coder {list,set,unset,set-default}`.
- Skill: `/consultants config` → "Coder routing" subflow.

---

## Stall thresholds

The stall suite (M11a) gates per-model `stall_threshold_s` and
`hard_cap_s` defaults in
[`consultants/engine/stall_defaults.py`](../consultants/engine/stall_defaults.py).

| Date       | Suite ver. | Model                          | p99 TTFT (ms) | p99 inter (ms) | p99 wall (s) | recommended stall_s | recommended hard_cap_s | Suite hash | Notes |
|------------|-----------:|--------------------------------|--------------:|---------------:|-------------:|--------------------:|-----------------------:|------------|-------|
| 2026-05-17 |        1.0 | `glm-5.1:cloud`                |       29 891 |          1 269 |         41.6 |                  90 |                    300 | `c8306c62` | Tier-1 only; balanced startup + cadence |
| 2026-05-17 |        1.0 | `kimi-k2.6:cloud`              |    **150 376** |            815 |        166.7 |             **390** |                    540 | `c8306c62` | **stall_s > global 300 — current default would falsely STARTUP_STALL on kimi cold calls.** Headline M11a finding. |
| 2026-05-17 |        1.0 | `gemma4:31b-cloud`             |         5 192 |       **3 895** |         82.6 |                  30 |                    300 | `c8306c62` | Fast startup but heavy inter-token spikes — clamped at 30 s stall floor |
| 2026-05-17 |        1.0 | `qwen3-coder-next:cloud`       |           390 |            699 |         22.2 |                  30 |                    300 | `c8306c62` | Fastest in cohort; clamps at floor on both axes |
| 2026-05-17 |        1.0 | `deepseek-v4-pro:cloud`        |        54 504 |            240 |         79.5 |                 150 |                    300 | `c8306c62` | Cleanest cadence in cohort (p99 inter just 240 ms) |
| 2026-05-17 |        1.0 | `deepseek-v4-flash:cloud`      |        79 662 |       **4 534** |    **255.5** |                 210 |                **780** | `c8306c62` | Big inter-token spike + slowest p99 wall; raised hard_cap above floor |
| 2026-05-17 |        1.0 | `gemini-3-flash-preview:cloud` |         6 828 |            266 |         14.9 |                  30 |                    300 | `c8306c62` | Snappiest among the "slow startup" cohort — surprising; cold-start TTFT only ~5 s |

### Methodology

Suite v1.0 (8 questions: 4 standalone + 2 council-synth + 2
council-gpqa). **This baseline is Tier-1 only** (84 trials =
7 models × 4 standalone questions × 3 trials). Tier 2
(fake-consultancy) is deferred — Tier 1 already produced clearly
differentiated per-model recommendations. If a model's real-world
usage flags problems, re-run with `--tier2` or `--both` to refine
under realistic council load.

Derivation rule:
```
stall_threshold_s = max(p99_inter_token_ms, p99_ttft_ms) * 2.5,
                    rounded up to the nearest 30 s,
                    floored at 30 s, ceiled at 600 s.
hard_cap_s        = p99(wall_s) * 3.0,
                    rounded up to the nearest 60 s,
                    floored at 300 s, ceiled at 3600 s.
```

Multipliers + clamps live in
[`benchmarks/consultants/questions/stall/SUITE.md`](../benchmarks/consultants/questions/stall/SUITE.md)
under `rubric:`.

### Out-of-cohort models

Models NOT in the table above fall through to
`RECOMMENDED_DEFAULT_STALL = (300, 3600)` — the existing global
defaults from `consultants/engine/control.py`. To bake a new
model's threshold in, add it to the cohort and re-run:

```bash
claude-consultants skill-eval stall --live --tier1 \
    --models <existing-cohort>,<new-model:v0> \
    --accept-cost
```

Or override at runtime with a TOML config:

```toml
[runtime]
stall_threshold_s = 500   # wins over stall_defaults.py
```

### Bench artifacts

- Results dir: [`benchmarks/consultants/results/2026-05-17/stall-tier1/`](../benchmarks/consultants/results/2026-05-17/stall-tier1/)
- Bench commit: `bcb1885` (M11a-1 harness landing).
- Live-run commit: see the M11a-2 commit that appended this section.

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
