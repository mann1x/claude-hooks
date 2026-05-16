# Skill-Eval Report — coder suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder` |
| suite_version | `1.0` |
| suite_hash | `9aa6eaf011d2a8b3...` |
| released | `2026-05-16` |
| run_started_at | `2026-05-16T15:20:21.166566Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `25ed2d4` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | 2 | 100% (2/2) | 100% (2/2) | 12.4s | 2165 | 5.00 | ✅ |
| `deepseek-v4-pro:cloud` | 2 | 100% (2/2) | 100% (2/2) | 11.5s | 2142 | 4.50 | ✅ |
| `minimax-m2.7:cloud` | 2 | 100% (2/2) | 100% (2/2) | 6.2s | 1589 | 4.00 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**minimax-m2.7:cloud** wins the rubric: pass_rate=100%, avg_quality=4.00, median_tokens=1589. Also qualifying: deepseek-v4-pro:cloud (100%/4.50), deepseek-v4-flash:cloud (100%/5.00).

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### trivial

**trivial-01-truncate**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2247 | 5.0 | 6 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2191 | 5.0 | 6 |
| `minimax-m2.7:cloud` | PASS | 2 | 1765 | 3.0 | 9 |

**trivial-02-strlen**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `deepseek-v4-flash:cloud` | PASS | 2 | 2084 | 5.0 | 5 |
| `deepseek-v4-pro:cloud` | PASS | 2 | 2093 | 4.0 | 5 |
| `minimax-m2.7:cloud` | PASS | 2 | 1414 | 5.0 | 5 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`9aa6eaf011d2` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
