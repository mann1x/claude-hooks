# Tier 2 — stale-doc question, 3 trials per arm, effort=high

The n=1 run answered a question with nothing to catch. This one seeds
a confident, stale `RETRY-DESIGN.md` next to the code, stating four
values the code contradicts, and asks a question those values answer.

Arms differ only in `[tools] all_roles`, each in its own project dir,
and the two arms of a trial run **concurrently** so cloud latency
cannot drift between them.

## Cost — the delta is real and reproducible

```
metric              off                      on                    delta
LLM calls            18 [15–19]               19 [16–21]            +8%
prompt tokens   186,309 [158,794–210,185] 129,514 [102,216–149,424] -30%
completion       12,160 [11,664–12,952]    10,535 [9,562–11,365]    -13%
tool calls           27 [22–33]               24 [22–26]             -9%
wall (s)          1,922                     1,938                   +0.8%
```

**The token ranges do not overlap.** Off's cheapest trial (158,794
prompt / 11,664 completion) still costs more than on's most expensive
(149,424 / 11,365). At n=3 that is a much stronger claim than the n=1
run could make: this is not a mean hiding a split.

Per role (mean calls / mean completion tokens):

| role | off | on |
|---|---|---|
| planner | 1.0 / 631 | **3.3 / 948** |
| researcher | 14.7 / 10,230 | **12.7 / 7,967** |
| critic | 1.0 / 506 | **2.0 / 749** |
| synthesizer | 1.0 / 793 | 1.0 / 870 |

Same mechanism as the n=1 run, now with three samples: the tooled
planner looks at the code before writing the plan, and the researcher
then converges in ~2 fewer iterations. Prompt tokens fall further than
call count because a tool loop resends its whole history each
iteration — the last iterations carry the largest prompts, so removing
two of them removes more than 2/15ths of the cost.

Wall is unchanged (+0.8%, well inside the noise of a ~32 min run
dominated by cloud latency).

## Correctness — the trap did not spring, in either arm

| fact | off (true/false/absent) | on (true/false/absent) |
|---|---|---|
| deadline_s | **3/0/0** | **3/0/0** |
| max_attempts | **3/0/0** | **3/0/0** |
| breaker_enabled | **3/0/0** | **3/0/0** |
| base_delay_s | 0/0/3 | 0/0/3 |

Every trial in **both** arms stated the code's values. Not one repeated
the doc. (`base_delay_s` is `absent` everywhere because the question
did not ask about backoff — correctly not scored as a failure.)

This is a negative result for the hypothesis, and the reason matters:
**the researcher read `RETRY-DESIGN.md` and cross-checked it anyway.**
Transcript of the *untooled* arm shows 33 tool calls, including a read
of the design doc, followed by reads of `retry.py` — and it reported
90.0 / 15 / disabled.

So the premise behind this question was wrong. The researcher has tools
in *both* arms — it always did; that is what M-B changes for the *other*
roles — so a stale doc sitting next to readable code does not produce
wrong research. Planting a doc is not enough to make the research wrong
when the researcher can read the code the doc describes.

## What the two tiers together now say

- **Tier 1** (critic driven directly, planted-false research): a tooled
  critic catches 100% of planted falsehoods vs 0% untooled, with no
  false positives and no silent corrections. That benefit is real
  *when the research is wrong*.
- **Tier 2** (full council, stale doc): the research was not wrong, so
  the critic had nothing to catch — and the measured benefit of
  `all_roles` came from somewhere else entirely, the **planner**
  grounding its plan and shortening the research loop.

The honest reading: at council scale `all_roles` pays for itself
through **efficiency**, not through **correctness**. The correctness
case is demonstrated only at Tier 1, under conditions Tier 2 could not
reproduce naturally.

## Still not established

A council-level correctness benefit. To exercise it the research itself
has to be wrong, and the natural cause (a stale doc) is defeated by the
researcher's own tools. Options for a future pass, none of them free of
artifact:

1. Degrade the researcher (drop its tools, or force REPORT-only mode)
   so the doc is its only source — but then the comparison is against a
   council nobody runs.
2. Use a question whose ground truth is genuinely hard to read from
   code — dynamic dispatch, config-driven behaviour, generated code —
   where a doc plausibly beats a grep.
3. Accept Tier 1 as the correctness evidence and Tier 2 as the cost
   evidence, which is what the data currently supports.

## Reproduce

```
python -m consultants.server --host 127.0.0.1 --port 38195 &
python -m benchmarks.consultants.role_tools_bench --tier cost --trials 3 \
    --project <workdir> --seed-dir <code + RETRY-DESIGN.md> \
    --base http://127.0.0.1:38195 --effort high --question "..." --out <dir>
```
