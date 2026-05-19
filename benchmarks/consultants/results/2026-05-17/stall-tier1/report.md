# Stall Skill-Eval Report — suite v1.0 (hash c8306c62)

- **Mode:** `live`
- **Ollama base:** `http://192.168.178.2:11433`
- **Trials run:** 84
- **Models:** 7
- **Harness version:** 1.0

## Per-model summary

| Model | trials | T1 | T2 | p50 TTFT (ms) | p99 TTFT (ms) | p50 inter (ms) | p99 inter (ms) | p99 wall (s) | → stall_s | → hard_cap_s |
|-------|-------:|---:|---:|--------------:|--------------:|---------------:|---------------:|-------------:|---------:|------------:|
| `glm-5.1:cloud` | 12 | 12 | 0 | 18995 | 29891 | 0.0 | 1269.0 | 41.6 | 90 | 300 |
| `kimi-k2.6:cloud` | 12 | 12 | 0 | 57419 | 150376 | 4.2 | 814.8 | 166.7 | 390 | 540 |
| `gemma4:31b-cloud` | 12 | 12 | 0 | 382 | 5192 | 40.0 | 3894.7 | 82.6 | 30 | 300 |
| `qwen3-coder-next:cloud` | 12 | 12 | 0 | 275 | 390 | 0.0 | 699.2 | 22.2 | 30 | 300 |
| `deepseek-v4-pro:cloud` | 12 | 12 | 0 | 24009 | 54504 | 14.0 | 240.1 | 79.5 | 150 | 300 |
| `deepseek-v4-flash:cloud` | 12 | 12 | 0 | 24510 | 79662 | 25.8 | 4534.1 | 255.5 | 210 | 780 |
| `gemini-3-flash-preview:cloud` | 12 | 12 | 0 | 5373 | 6828 | 148.6 | 266.0 | 14.9 | 30 | 300 |

## Recommended `stall_defaults.py` (M11a-2 closeout)

Append these entries to ``RECOMMENDED_STALL_THRESHOLDS_BY_MODEL``:

```python
    "glm-5.1:cloud": StallThresholds(stall_threshold_s=90, hard_cap_s=300),
    "kimi-k2.6:cloud": StallThresholds(stall_threshold_s=390, hard_cap_s=540),
    "gemma4:31b-cloud": StallThresholds(stall_threshold_s=30, hard_cap_s=300),
    "qwen3-coder-next:cloud": StallThresholds(stall_threshold_s=30, hard_cap_s=300),
    "deepseek-v4-pro:cloud": StallThresholds(stall_threshold_s=150, hard_cap_s=300),
    "deepseek-v4-flash:cloud": StallThresholds(stall_threshold_s=210, hard_cap_s=780),
    "gemini-3-flash-preview:cloud": StallThresholds(stall_threshold_s=30, hard_cap_s=300),
```

## Errors

No errored trials.

