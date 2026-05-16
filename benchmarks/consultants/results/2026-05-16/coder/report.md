# Skill-Eval Report — coder suite v1.0

## Provenance

| Field | Value |
|---|---|
| harness_version | `1.0` |
| suite | `coder` |
| suite_version | `1.0` |
| suite_hash | `9aa6eaf011d2a8b3...` |
| released | `2026-05-16` |
| run_started_at | `2026-05-16T14:13:28.525976Z` |
| mode | `live` |
| ollama_base | `http://192.168.178.2:11433` |
| judge_model | `kimi-k2.6:cloud` |
| git_commit | `554a354` |
| host | `solidpc` |

## Per-model summary

Rubric: `pass_rate ≥ 70%` **AND** `avg_quality ≥ 3.5`. Tie-broken by `median_tokens` (lower wins).

| Model | Trials | Pass rate | Compile rate | Median wall | Median tokens | Avg quality | Qualifies? |
|---|---|---|---|---|---|---|---|
| `gemma4:31b-cloud` | 8 | 100% (8/8) | 100% (8/8) | 14.5s | 1876 | 4.62 | ✅ |
| `glm-5.1:cloud` | 8 | 100% (8/8) | 100% (8/8) | 4.9s | 1841 | 4.88 | ✅ |
| `kimi-k2.6:cloud` | 8 | 100% (8/8) | 100% (8/8) | 10.0s | 1957 | 4.86 | ✅ |
| `qwen3-coder-next:cloud` | 8 | 100% (8/8) | 100% (8/8) | 2.9s | 2111 | 4.50 | ✅ |

## Recommended default for `cfg.roles.coder.model`

**glm-5.1:cloud** wins the rubric: pass_rate=100%, avg_quality=4.88, median_tokens=1841. Also qualifying: gemma4:31b-cloud (100%/4.62), kimi-k2.6:cloud (100%/4.86), qwen3-coder-next:cloud (100%/4.50).

To adopt this default, update `consultants/engine/coder_defaults.py` (or `cfg.roles.coder.model` in the config TOML) and record this score in `docs/consultants-skill-eval-baselines.md`.

## Per-question detail

### trivial

**trivial-01-truncate**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1905 | 5.0 | 6 |
| `glm-5.1:cloud` | PASS | 2 | 1744 | 5.0 | 6 |
| `kimi-k2.6:cloud` | PASS | 2 | 1975 | 5.0 | 6 |
| `qwen3-coder-next:cloud` | PASS | 2 | 1983 | 5.0 | 6 |

**trivial-02-strlen**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1579 | 5.0 | 5 |
| `glm-5.1:cloud` | PASS | 2 | 1643 | 5.0 | 2 |
| `kimi-k2.6:cloud` | PASS | 2 | 1829 | — | 2 |
| `qwen3-coder-next:cloud` | PASS | 2 | 1952 | 4.0 | 6 |

### easy

**easy-01-dedupe**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1605 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1692 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 1699 | 5.0 | 8 |
| `qwen3-coder-next:cloud` | PASS | 2 | 1954 | 5.0 | 8 |

**easy-02-fib**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1799 | 4.0 | 9 |
| `glm-5.1:cloud` | PASS | 2 | 1863 | 5.0 | 7 |
| `kimi-k2.6:cloud` | PASS | 2 | 1818 | 5.0 | 7 |
| `qwen3-coder-next:cloud` | PASS | 2 | 2183 | 4.0 | 11 |

### medium

**medium-01-balance**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1957 | 5.0 | 11 |
| `glm-5.1:cloud` | PASS | 2 | 1865 | 5.0 | 11 |
| `kimi-k2.6:cloud` | PASS | 2 | 1940 | 5.0 | 12 |
| `qwen3-coder-next:cloud` | PASS | 2 | 2110 | 5.0 | 10 |

**medium-02-prime-length**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 1847 | 5.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1819 | 5.0 | 8 |
| `kimi-k2.6:cloud` | PASS | 2 | 1992 | 5.0 | 14 |
| `qwen3-coder-next:cloud` | PASS | 2 | 2113 | 5.0 | 14 |

### hard

**hard-01-matrix-path**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 2611 | 5.0 | 19 |
| `glm-5.1:cloud` | PASS | 2 | 2288 | 5.0 | 24 |
| `kimi-k2.6:cloud` | PASS | 2 | 2792 | 5.0 | 20 |
| `qwen3-coder-next:cloud` | PASS | 2 | 2505 | 4.0 | 17 |

**hard-02-digit-filter**

| Model | Status | Iters | Tokens | Quality | Code lines |
|---|---|---|---|---|---|
| `gemma4:31b-cloud` | PASS | 2 | 2912 | 3.0 | 8 |
| `glm-5.1:cloud` | PASS | 2 | 1992 | 4.0 | 9 |
| `kimi-k2.6:cloud` | PASS | 2 | 2869 | 4.0 | 11 |
| `qwen3-coder-next:cloud` | PASS | 2 | 2259 | 4.0 | 11 |

## Reproducibility

Re-run this exact suite version:

```
python benchmarks/consultants/coder_bench.py \
    --live --accept-cost \
    --models kimi-k2.6:cloud,qwen3-coder-next:cloud,glm-5.1:cloud,gemma4:31b-cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

If `suite_hash` differs from this run's (`9aa6eaf011d2` if recorded), the question content drifted without a SUITE.md version bump — investigate before comparing baselines.

Add the line for this run to `docs/consultants-skill-eval-baselines.md` so future-you can compare new candidates against today's numbers.
