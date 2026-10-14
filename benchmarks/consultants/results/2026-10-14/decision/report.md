# Decision-model bench — 2026-10-14

25 council-shaped cases × 2 repeat(s), one request per case. Confidence floor 0.6 (a noul at |2p−1|, as the fork reads it).

## By target

| target | accuracy | mean Brier | confident & wrong | median ms | p90 ms | errors |
|---|---|---|---|---|---|---|
| `typesafe:jev-latest` | 50/50 | 0.017 | 0 | 262 | 299 | 0 |
| `typesafe:jev-preview` | 50/50 | 0.018 | 0 | 270 | 303 | 0 |
| `xollama:tev1:latest` | 44/50 | 0.114 | 0 | 144 | 169 | 0 |
| `xollama:nimble:latest` | 42/50 | 0.144 | 4 | 191 | 225 | 0 |

## Accuracy by seam

| seam | `typesafe:jev-latest` | `typesafe:jev-preview` | `xollama:tev1:latest` | `xollama:nimble:latest` |
|---|---|---|---|---|
| review-accept | 10/10 | 10/10 | 8/10 | 8/10 |
| citation-check | 12/12 | 12/12 | 12/12 | 12/12 |
| struggle-supervisor | 8/8 | 8/8 | 8/8 | 8/8 |
| compaction-review | 8/8 | 8/8 | 6/8 | 6/8 |
| review-loop | 6/6 | 6/6 | 6/6 | 6/6 |
| critic-severity | 6/6 | 6/6 | 4/6 | 2/6 |

## Misses

- `xollama:tev1:latest` answers-3 (truth False): p=0.5246, confidence=0.0492
- `xollama:tev1:latest` kept-2 (truth False): p=0.6512, confidence=0.3024
- `xollama:tev1:latest` sev-2 (truth 0): score=0.529, confidence=0.412
- `xollama:tev1:latest` answers-3 (truth False): p=0.5246, confidence=0.0492
- `xollama:tev1:latest` kept-2 (truth False): p=0.6512, confidence=0.3024
- `xollama:tev1:latest` sev-2 (truth 0): score=0.529, confidence=0.412
- `xollama:nimble:latest` answers-3 (truth False): p=0.6998, confidence=0.3997
- `xollama:nimble:latest` kept-2 (truth False): p=0.9846, confidence=0.9693
- `xollama:nimble:latest` sev-1 (truth 3): score=2.462, confidence=0.4947
- `xollama:nimble:latest` sev-2 (truth 0): score=0.837, confidence=0.6515
- `xollama:nimble:latest` answers-3 (truth False): p=0.6998, confidence=0.3997
- `xollama:nimble:latest` kept-2 (truth False): p=0.9846, confidence=0.9693
- `xollama:nimble:latest` sev-1 (truth 3): score=2.462, confidence=0.4947
- `xollama:nimble:latest` sev-2 (truth 0): score=0.837, confidence=0.6515
