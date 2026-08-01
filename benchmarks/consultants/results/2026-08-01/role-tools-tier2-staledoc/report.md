# M-B Tier 2 — council cost A/B (stale-doc question)

effort=high  trials=3 per arm  question hash=90d3ae6cbfff

Means across trials; `[min–max]` is the per-trial spread.

| metric | all_roles off | all_roles on | delta |
|---|---|---|---|
| LLM calls | 18 [15–19] | 19 [16–21] | +8% |
| prompt tokens | 186309 [158794–210185] | 129514 [102216–149424] | -30% |
| completion tokens | 12160 [11664–12952] | 10535 [9562–11365] | -13% |
| tool calls | 27 [22–33] | 24 [22–26] | -9% |
| wall (s) | 1922 | 1938 |  |

## Correctness — did the stale doc reach the answer?

The seeded `RETRY-DESIGN.md` states four values the code
contradicts. `true` = the answer states the code's value;
`false` = it repeats the doc; `absent` = it did not commit
either way, which is neither a catch nor a failure.

| fact | off (true/false/absent) | on (true/false/absent) |
|---|---|---|
| deadline_s | 3/0/0 | 3/0/0 |
| max_attempts | 3/0/0 | 3/0/0 |
| base_delay_s | 0/0/3 | 0/0/3 |
| breaker_enabled | 3/0/0 | 3/0/0 |

## Per role (mean LLM calls / mean completion tokens)

| role | off | on |
|---|---|---|
| critic | 1.0 / 506 | 2.0 / 749 |
| planner | 1.0 / 631 | 3.3 / 948 |
| researcher | 14.7 / 10230 | 12.7 / 7967 |
| synthesizer | 1.0 / 793 | 1.0 / 870 |
