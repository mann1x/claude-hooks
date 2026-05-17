# Skill-Eval Report — coder_mlang suite v1.0.1

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder_mlang` |
| suite_version | `1.0.1` |
| suite_hash | `ddef8095311c258d...` |
| released | `2026-05-17` |
| run_started_at | `2026-05-17T11:46:51.780957Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `1729324` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `glm-5.1:cloud` | 1 | 100% (1/1) | 100% (1/1) | 6.1s | 2867 | _n/a_ | ❌ |

## Recommended default for `cfg.roles.coder.model`

no model meets the rubric (pass_rate ≥ 70%, quality ≥ 3.5). Best so far: **glm-5.1:cloud** with pass_rate=100%, avg_quality=n/a. The role's default stays at the project-global DEFAULT_MODEL until a follow-up run produces a qualifying candidate.

## Per-question detail

### hard

**c-hard-01-quicksort-3way**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `glm-5.1:cloud` | PASS | 2 | 2867 | — | 72 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models glm-5.1:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`ddef8095311c` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
