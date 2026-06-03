# Skill-Eval Report — coder suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder` |
| suite_version | `1.0` |
| suite_hash | `2103e6cc4cf3c635...` |
| released | `2026-05-16` |
| run_started_at | `2026-06-03T22:25:31.577190Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `d0fc724` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `minimax-m3:cloud` | 8 | 100% (8/8) | 100% (8/8) | 7.7s | 2528 | 4.25 | ✅ |
| `nemotron-3-super:cloud` | 8 | 100% (8/8) | 100% (8/8) | 2.7s | 2406 | 4.38 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**nemotron-3-super:cloud** wins the rubric: pass_rate=100%, avg_quality=4.38, median_tokens=2406. Also qualifying: minimax-m3:cloud (100%/4.25).

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### trivial

**trivial-01-truncate**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2478 | 3.0 | 16 |
| `nemotron-3-super:cloud` | PASS | 2 | 2293 | 5.0 | 6 |

**trivial-02-strlen**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2226 | 5.0 | 5 |
| `nemotron-3-super:cloud` | PASS | 2 | 2165 | 5.0 | 2 |

### easy

**easy-01-dedupe**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2579 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2244 | 5.0 | 9 |

**easy-02-fib**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2609 | 5.0 | 7 |
| `nemotron-3-super:cloud` | PASS | 2 | 2460 | 4.0 | 11 |

### medium

**medium-01-balance**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2411 | 5.0 | 12 |
| `nemotron-3-super:cloud` | PASS | 2 | 2352 | 5.0 | 11 |

**medium-02-prime-length**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2463 | 4.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2701 | 4.0 | 15 |

### hard

**hard-01-matrix-path**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 3684 | 4.0 | 27 |
| `nemotron-3-super:cloud` | PASS | 2 | 2900 | 4.0 | 24 |

**hard-02-digit-filter**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `minimax-m3:cloud` | PASS | 2 | 2725 | 3.0 | 14 |
| `nemotron-3-super:cloud` | PASS | 2 | 2578 | 3.0 | 12 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models minimax-m3:cloud,nemotron-3-super:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`2103e6cc4cf3` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
