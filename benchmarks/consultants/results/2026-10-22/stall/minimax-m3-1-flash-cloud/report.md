# Stall Skill-Eval Report — suite v1.0 (hash c8306c62)

- **Mode:** `live`
- **Ollama base:** `http://192.168.178.161:11434`
- **Trials run:** 12
- **Models:** 1
- **Harness version:** 1.0

## Per-model summary

| Model | trials | T1 | T2 | p50 TTFT (ms) | p99 TTFT (ms) | p50 inter (ms) | p99 inter (ms) | p99 wall (s) | → stall_s | → hard_cap_s |
|-------|-------:|---:|---:|--------------:|--------------:|---------------:|---------------:|-------------:|---------:|------------:|
| `minimax-m3.1-flash:cloud` | 12 | 12 | 0 | 861 | 1501 | 249.1 | 612.5 | 260.8 | 30 | 840 |

## Recommended `stall_defaults.py` (M11a-2 closeout)

Append these entries to ``RECOMMENDED_STALL_THRESHOLDS_BY_MODEL``:

```python
    "minimax-m3.1-flash:cloud": StallThresholds(stall_threshold_s=30, hard_cap_s=840),
```

## Errors

No errored trials.

