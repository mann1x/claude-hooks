# M-B Tier 2 — council cost A/B

effort=high  question hash=144d43130abd

| metric | all_roles off | all_roles on | delta |
|---|---|---|---|
| LLM calls | 20 | 22 | +10% |
| prompt tokens | 297704 | 273786 | -8% |
| completion tokens | 14465 | 13408 | -7% |
| tool calls | 25 | 27 | +8% |
| wall (s) | 1907.8 | 1916.1 |  |

Per role (LLM calls / completion tokens):

| role | off | on |
|---|---|---|
| critic | 1 / 132 | 2 / 826 |
| planner | 1 / 485 | 4 / 1015 |
| researcher | 17 / 12877 | 15 / 10978 |
| synthesizer | 1 / 971 | 1 / 589 |
