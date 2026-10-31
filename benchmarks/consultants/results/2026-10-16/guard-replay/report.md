# Guard replay (2026-10-16)

207 transcripts, 911 tool-loop lanes ({'researcher': 875, 'tool_executor': 26, 'coder': 10}), 6282 turns (4117 with reasoning text), 14233 tool calls. Commit v1.20.2-62-gf76fa09-dirty. Labels: the lane's next 6 calls after a stop (stuck ≤ 1/6 new, progressing ≥ 1/2 new, ended < 3 calls left).

## At the shipped thresholds

| guard | events |
|---|---|
| loop_guard | last_warning:consecutive 5, stop:ended 1, stop:stuck 4, warn:barren 21, warn:consecutive 19, warn:result_cycle 44 |
| mistake_limit | stop:ended 3 |
| reasoning_loop | cut:block-cycle 7 |
| repetition | nudge 1 |
| silent_turn | announced_intent 4, unparsed_call 57 |
| struggle | nudge:non-progress:ended 3, nudge:struggle:ended 4, nudge:struggle:progressing 2, nudge:struggle:stuck 4 |

## Sweeps (one threshold at a time; others at their defaults)

| setting | warn | last warning | cut | nudge | stop: stuck | stop: ended | stop: mixed | stop: progressing | new calls lost |
|---|---|---|---|---|---|---|---|---|---|
| loop_guard HARD_THRESHOLD=3 | 71 | 8 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard HARD_THRESHOLD=4 | 79 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard HARD_THRESHOLD=5 | 84 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard HARD_THRESHOLD=6 | 89 | 5 | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| loop_guard HARD_THRESHOLD=8 | 98 | 4 | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| loop_guard STRIKE_LIMIT=3 | 83 | 6 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard STRIKE_LIMIT=4 | 84 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard STRIKE_LIMIT=6 | 84 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard STRIKE_LIMIT=8 | 84 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard CYCLE_REPEAT_LIMIT=2 | 187 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard CYCLE_REPEAT_LIMIT=3 | 84 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard CYCLE_REPEAT_LIMIT=4 | 61 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| loop_guard CYCLE_REPEAT_LIMIT=5 | 52 | 5 | 0 | 0 | 4 | 1 | 0 | 0 | 0 |
| mistake_limit=3 | 0 | 0 | 0 | 0 | 7 | 3 | 1 | 36 | 598 |
| mistake_limit=4 | 0 | 0 | 0 | 0 | 3 | 2 | 3 | 18 | 334 |
| mistake_limit=5 | 0 | 0 | 0 | 0 | 3 | 3 | 0 | 7 | 124 |
| mistake_limit=6 | 0 | 0 | 0 | 0 | 3 | 2 | 0 | 3 | 38 |
| mistake_limit=8 | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 2 | 28 |
| mistake_limit=10 | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 |
| reasoning_loop min_cycles=10 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop min_cycles=15 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop min_cycles=20 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop min_cycles=30 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop max_unique_in_window=2 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop max_unique_in_window=4 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop max_unique_in_window=6 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop max_unique_in_window=8 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop phrase_min_cycles=6 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop phrase_min_cycles=8 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop phrase_min_cycles=12 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop phrase_min_cycles=16 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop min_chars=2000 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop min_chars=4000 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| reasoning_loop min_chars=8000 | 0 | 0 | 7 | 0 | 0 | 0 | 0 | 0 | 0 |
| struggle BUDGET_TURNS_TO_NUDGE=2 | 0 | 0 | 0 | 13 | 0 | 0 | 0 | 0 | 0 |
| struggle BUDGET_TURNS_TO_NUDGE=3 | 0 | 0 | 0 | 13 | 0 | 0 | 0 | 0 | 0 |
| struggle BUDGET_TURNS_TO_NUDGE=4 | 0 | 0 | 0 | 13 | 0 | 0 | 0 | 0 | 0 |
| struggle FAILURES_TO_NUDGE=2 | 0 | 0 | 0 | 41 | 0 | 0 | 0 | 0 | 0 |
| struggle FAILURES_TO_NUDGE=3 | 0 | 0 | 0 | 13 | 0 | 0 | 0 | 0 | 0 |
| struggle FAILURES_TO_NUDGE=4 | 0 | 0 | 0 | 9 | 0 | 0 | 0 | 0 | 0 |
| struggle FAILURES_TO_NUDGE=5 | 0 | 0 | 0 | 8 | 0 | 0 | 0 | 0 | 0 |
| struggle MIN_ITERATION=4 | 0 | 0 | 0 | 25 | 0 | 0 | 0 | 0 | 0 |
| struggle MIN_ITERATION=8 | 0 | 0 | 0 | 13 | 0 | 0 | 0 | 0 | 0 |
| struggle MIN_ITERATION=12 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |

## Findings and what changed (plan D acceptance: thresholds fitted by replay)

The first pass ran the guards as shipped at f76fa09; the tables above are the
refit. Counts are over the same 911 lanes.

| guard | as shipped (f76fa09) | refit | change |
|---|---|---|---|
| D1 reasoning loop | 0 trips at every setting swept; six turns of 190k–250k characters whose second half is one 34–62-line block repeated verbatim (≈50k tokens each) all pass | 7 cuts, all `block-cycle`, tripping at 40k–100k characters | new `block-cycle` signal: 7–128-line exact period, ≥ 3 repeats, ≥ 6,000 repeated characters, checked every 16 lines; stable over 2–4 repeats and 3k–12k characters (6–7 trips, the same blocks); 0.3 s over all 4,117 blocks |
| D4 / D3 call left in text | 57 lanes (25 councils, all `minimax-m3:cloud`) ended on a turn holding `<tool_call>` + `<invoke name="…">` markup, read as the lane's report | 57 re-asked (`unparsed_call`) | `silent_turns.unparsed_tool_call` reads MiniMax's `<invoke name=…>` form |
| D3 promise of work | 8 re-asks, 2 of them on a finished report ("… and **lets** Claude Code re-issue", "would **let me** cite") | 4 re-asks, none on a report | "let's" needs its apostrophe (or a work verb after "lets"); "let me" after would / could / might / should / to is a condition |
| D2 length nudge | 443 nudges on the count alone: 204 on lanes whose next calls were mostly new, 5 on stuck lanes | 3 | also needs ≤ 1 of the last 6 calls new (`STALE_WINDOW` / `STALE_MAX_NEW`) |
| D2 failure nudge | hidden behind the length nudge (one nudge per lane) | 10: 4 stuck, 4 ended, 2 progressing | unchanged; a nudge, never a stop |
| D14 mistake limit | 6: 8 stops, 3 on lanes still progressing (wrong-path streaks the model fixed itself; one lost 23 new calls); 20 stale calls saved against 38 new calls cut | 10: 3 stops, all on lanes ending anyway | every value under 10 cut more new calls than it saved stale ones |
| D10 loop guard | 5 stops: 4 stuck, 1 ended; none progressing | same | kept (5 / 6 / 3); `HARD_THRESHOLD` 6+ only delays the same stops |

Limits: the replay reads lanes as they ran, so what a nudge or a cut would
have changed afterwards is not observed; a stop's label is what the lane did
next without it. The capped-thinking stop (D2) never fired: few recorded
replies carry a budget message or a `num_predict` near the thinking length.
