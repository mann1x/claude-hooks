---
suite: stall
suite_version: "1.0"
released: 2026-05-17
manifest:
  - standalone-01-research-synth
  - standalone-02-compare-architectures
  - standalone-03-summarize-paper
  - standalone-04-explain-tradeoff
  - council-synth-01-proxy-log-rca
  - council-synth-02-two-stack-tradeoff
  - council-gpqa-01-physics
  - council-gpqa-02-biology
rubric:
  stall_margin_factor: 2.5
  hardcap_margin_factor: 3.0
  stall_min_s: 30
  stall_max_s: 600
  stall_round_s: 30
  hardcap_min_s: 300
  hardcap_max_s: 3600
  hardcap_round_s: 60
---

# Stall Skill-Eval Suite v1.0

This file is the **manifest** for the stall sub-protocol of the
**Consultancy Skill-Eval Protocol**. See
[`docs/consultants-skill-eval-protocol.md`](../../../../docs/consultants-skill-eval-protocol.md)
for the methodology, when-to-rerun rules, and the full decision
framework.

## What this suite measures

A candidate model's **streaming-token cadence under realistic
workload**. Three signals per (question × model × trial):

- **time-to-first-token (TTFT)** — wall clock between request
  submission and the first content token. Sets the lower bound
  on the `STARTUP_STALL` threshold.
- **inter-token gap p99** — the 99th-percentile gap between
  consecutive tokens during normal generation. Sets the
  `MID_STREAM_STALL` threshold.
- **total wall p99** — the 99th-percentile total response time.
  Sets the `HARD_CAP_EXCEEDED` ceiling.

The bench feeds these three numbers into a derivation rule
encoded in `benchmarks/consultants/stall_bench.py:derive_thresholds`
and the rubric block above:

```
stall_threshold_s = max(p99_inter_token_ms, p99_ttft_ms) * 2.5,
                    in seconds, rounded up to the nearest 30s,
                    floored at 30s, ceiled at 600s.
hard_cap_s        = p99(wall_s) * 3.0,
                    rounded up to the nearest 60s,
                    floored at 300s, ceiled at 3600s.
```

The rule's margin factors (`2.5×` for stall, `3.0×` for hard cap)
plus the floor/ceil clamps live in the rubric block of this file
so the M11a-2 closeout commit can tune them without code changes
if measured data calls for it.

## Two tiers

The suite splits into two **tiers** that exercise different
workload shapes — the M11a plan motivation explains why:

### Tier 1 — standalone (`standalone-*.md`)

One `chat_streamed` call per (question × model × trial_idx). The
bench feeds the question body + task directly to the chat client
under test. Cheap; produces a broad per-model cadence baseline
across the full cohort.

Questions are researcher-style analytical prompts: 600-1200 token
expected responses, multi-paragraph reasoning, no special
constraints.

### Tier 2 — council (`council-*.md`)

A full `build_council_graph` run per (question × model ×
trial_idx) at `effort="medium"`, with every chat client in
`GraphDeps` pinned to the same model so we measure that one
model's cadence under realistic council load (planner →
researcher rounds → critic → synthesizer).

Questions split into two flavors:
- **`council-synth-*`** — synthetic audit-style prompts modelled
  on the 2026-05-15 audit-session pathology (root-cause this log
  trace; compare these two architectures). Closer to real
  claude-hooks workloads.
- **`council-gpqa-*`** — sampled real GPQA-Diamond items (physics
  + biology) for an externally-anchored difficulty signal.

Tier 2 is more expensive per trial (a council run fires ~5-8
inner chat calls) but the per-model p99 it produces is more
representative of the workload where stalls actually happen.

## Manifest (8 questions × 2 tiers)

| Tier       | ID                                       | Source                       | Notes                                                  |
|------------|------------------------------------------|------------------------------|--------------------------------------------------------|
| standalone | standalone-01-research-synth             | synthetic                    | 3-source research synthesis                            |
| standalone | standalone-02-compare-architectures      | synthetic                    | Architectural compare-and-contrast                     |
| standalone | standalone-03-summarize-paper            | synthetic                    | Summarise + critique a paper abstract                  |
| standalone | standalone-04-explain-tradeoff           | synthetic                    | Multi-step technical tradeoff explanation              |
| council    | council-synth-01-proxy-log-rca           | synthetic (audit-style)      | Root-cause a streaming-proxy log trace                 |
| council    | council-synth-02-two-stack-tradeoff      | synthetic (audit-style)      | Compare two LLM-gateway stacks, recommend one          |
| council    | council-gpqa-01-physics                  | GPQA-Diamond                 | Sampled physics item (knowledge + reasoning)           |
| council    | council-gpqa-02-biology                  | GPQA-Diamond                 | Sampled biology item (knowledge + reasoning)           |

## Rubric (the decision)

Unlike the coder bench, this is **not** a pass/fail gate — every
candidate model gets recommended thresholds derived from its
measured percentiles. The rubric block above carries the margin
factors + clamps the derivation rule applies; tuning them is a
deliberate M11a-2 decision based on the live data, not a per-run
choice.

The bench's `report.md` ranks models by **stall safety margin** =
`(stall_threshold_s - p99_inter_token_s) / p99_inter_token_s` —
higher is better (more headroom). M11a-2 may add per-model
overrides for models whose measured ratio looks suspicious.

## Reproducibility

Each results file records `suite_version: "1.0"` + the
content-derived `suite_hash` (first 8 chars). Re-running v1.0 of
the suite against a model that was scored in a prior run should
produce statistically similar percentiles (modulo proxy-side flap
/ model-side temperature drift). If you see a >30% swing on the
same model between two v1.0 runs, investigate the proxy upstream
before trusting the new number.

## Versioning

The suite version follows semver-ish rules:

- **PATCH** (1.0 → 1.0.1): a question's prompt body gets edited
  but the intent + expected token range is unchanged. Old
  baselines stay comparable; report any score drift as model-side,
  not suite-side.
- **MINOR** (1.0 → 1.1): a new question is added OR an existing
  question's intent shifts (different workload pattern). Existing
  baselines are NOT comparable to the new suite version; re-run
  the cohort.
- **MAJOR** (1.0 → 2.0): the rubric block changes (margin
  factors, clamps, or the derivation rule). Existing baseline
  thresholds need to be recomputed from the raw per-trial data.

## Adding a new question

1. Pick a tier (`standalone` for cheap broad coverage,
   `council` for full-workload representativeness).
2. Add an `.md` file with the frontmatter shape used by the
   existing questions: `id`, `tier`, `source`, `task`.
3. Append the id to the `manifest:` list above in alphabetical
   order.
4. Bump the suite version per the rules above.
5. Run the live bench against the model cohort to refresh the
   baselines.

## Out of scope (deferred)

- **Live cloud calls** happen only in M11a-2 with explicit
  user opt-in via `--accept-cost`. M11a-1 ships the harness and
  fixtures only.
- **Multi-effort tiers** — Tier 2 runs at `effort="medium"`
  only. xmedium / xhigh / xmax run multi-model researcher fanout,
  which would conflate models we're trying to isolate.
- **Stress-test prompts** (very long generations designed to
  surface mid-stream stalls) — out of scope until M11a-2 says
  the v1.0 cadence data needs more headroom.
