# M-B Tier 2 — council cost A/B (stale-doc question)

effort=high  trials=3 per arm  question hash=90d3ae6cbfff

Means across trials; `[min–max]` is the per-trial spread.

| metric | typed off | typed on | delta |
|---|---|---|---|
| LLM calls | 27 [22–31] | 31 [27–36] | +17% |
| prompt tokens | 454139 [287267–581843] | 723250 [414636–981219] | +59% |
| completion tokens | 15280 [12448–16848] | 25005 [19024–29349] | +64% |
| tool calls | 43 [35–51] | 49 [43–55] | +13% |
| wall (s) | 1998 | 2019 |  |
| `[unverified` notes in the answer | 0.0 | 0.0 |  |

## Correctness — did the stale doc reach the answer?

The seeded `RETRY-DESIGN.md` states four values the code
contradicts. `true` = the answer states the code's value;
`false` = it repeats the doc; `absent` = it did not commit
either way, which is neither a catch nor a failure.

| fact | off (true/false/absent) | on (true/false/absent) |
|---|---|---|
| deadline_s | 3/0/0 | 3/0/0 |
| max_attempts | 3/0/0 | 3/0/0 |
| base_delay_s | 3/0/0 | 2/1/0 |
| breaker_enabled | 3/0/0 | 3/0/0 |

## Per role (mean LLM calls / mean completion tokens / prompt tokens per call / completion tokens per call)

| role | off | on |
|---|---|---|
| critic | 3.3 / 4443 / 10180 / 1333 | 4.7 / 7007 / 13384 / 1501 |
| planner | 3.3 / 212 / 1604 / 64 | 5.0 / 480 / 1761 / 96 |
| researcher | 19.0 / 9597 / 21548 / 505 | 20.7 / 16492 / 31163 / 798 |
| synthesizer | 1.0 / 1028 / 5439 / 1028 | 1.0 / 1027 / 7945 / 1027 |
