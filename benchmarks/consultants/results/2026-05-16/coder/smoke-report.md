# Skill-Eval Report — coder suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder` |
| suite_version | `1.0` |
| suite_hash | `9aa6eaf011d2a8b3...` |
| released | `2026-05-16` |
| run_started_at | `2026-05-16T14:06:45.362878Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `554a354` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `gemma4:31b-cloud` | 2 | 100% (2/2) | 100% (2/2) | 5.3s | 1770 | 5.00 | ✅ |
| `glm-5.1:cloud` | 2 | 100% (2/2) | 100% (2/2) | 4.0s | 1683 | 5.00 | ✅ |
| `kimi-k2.6:cloud` | 2 | 100% (2/2) | 100% (2/2) | 5.9s | 1697 | 3.50 | ✅ |
| `qwen3-next:cloud` | 2 | 0% (0/2) | 0% (0/2) | 0.2s | 0 | _n/a_ | ❌ |

## Recommended default for `cfg.roles.coder.model`

**glm-5.1:cloud** wins the rubric: pass_rate=100%, avg_quality=5.00, median_tokens=1683. Also qualifying: kimi-k2.6:cloud (100%/3.50), gemma4:31b-cloud (100%/5.00).

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### trivial

**trivial-01-truncate**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1917 | 5.0 | 6 |
| `glm-5.1:cloud` | PASS | 2 | 1731 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 1843 | 3.0 | 9 |
| `qwen3-next:cloud` | ERROR | 0 | 0 | — | 0 |

**trivial-02-strlen**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1624 | 5.0 | 2 |
| `glm-5.1:cloud` | PASS | 2 | 1635 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1551 | 4.0 | 5 |
| `qwen3-next:cloud` | ERROR | 0 | 0 | — | 0 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`9aa6eaf011d2` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
