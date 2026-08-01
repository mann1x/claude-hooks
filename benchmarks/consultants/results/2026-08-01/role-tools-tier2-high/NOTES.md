# Tier 2 — council cost A/B, effort=high

One run per arm. Same question, same isolated project (a copy of
`claude_hooks/proxy/`), same engine instance, arms differing only in
`[tools] all_roles`. Pipeline pinned to planner / researcher / critic /
synthesizer; `coder` and `tool_executor` off. Per-role `extra_models`
left as configured, so the x-tier fan-out stayed live.

Run against a **second engine instance on :38195 started from current
code** — the host's engine on :38095 had been up 40 days and would have
measured the pre-v1.3 prompts.

```
                    off        on      delta
LLM calls            20        22       +10%
prompt tokens   297,704   273,786        -8%
completion       14,465    13,408        -7%
tool calls           25        27        +8%
wall (s)         1907.8    1916.1       +0.4%
```

| role | off (calls / completion) | on (calls / completion) |
|---|---|---|
| planner | 1 / 485 | **4 / 1015** |
| researcher | 17 / 12877 | **15 / 10978** |
| critic | 1 / 132 | **2 / 826** |
| synthesizer | 1 / 971 | 1 / 589 |

Tool calls: off — researcher 25, nobody else. On — researcher 20,
planner 3, critic 4.

## The delta is negative, and the mechanism is visible

The tooled arm cost *less*. The planner and critic each did more work,
and the researcher did less: 2 fewer LLM calls and 5 fewer tool calls.

The transcript shows why. The tooled planner spent its three calls on
`survey_project`, `list_files proxy/`, and `read_file proxy/retry.py` —
it looked at the code before writing the plan, rather than pointing the
researcher at files it had inferred from names. The researcher then
converged in fewer rounds.

The critic's four calls were all `read_file` with explicit line ranges,
one per load-bearing cite the researcher had given
(`retry.py:373-375`, `retry.py:62-99`, `forwarder.py:327-449`,
`forwarder.py:582-598`). It checked the exact ranges claimed and
confirmed them. In the off arm the same critic spent 132 completion
tokens delivering a verdict on evidence it had no way to check.

No `CORRECTIONS` block appeared, which is correct: the researcher's
claims were accurate, so there was nothing to correct. The contract
says omit the block rather than write `CORRECTIONS: none`.

## What this does NOT establish

**n=1 per arm.** A council run is high-variance and this is a single
sample, so "all_roles is cheaper" is not a supportable claim from this
data. What it does support is the weaker, still useful claim that the
knob is **not obviously expensive** at council scale — which was the
open risk, and the risk that killed `tool_executor` (+12 min wall, +43%
tokens).

Two things make the pair more comparable than a naive n=1:

- Both arms reached the critic at **round 3**, i.e. the same number of
  research reroutes. Reroute count is a discrete branch that dominates
  researcher cost, so a mismatch there would have swamped everything
  else. It matched.
- Both ran one synthesizer call, same question, same fixture.

Two things still uncontrolled: cloud latency (wall is ~32 min per arm
and dominated by it, so the +0.4% wall delta is noise either way), and
sampling of the researcher's own agent loop.

**Before flipping the default**, this needs ≥3 runs per arm across ≥2
questions of different shapes — at minimum one where the research is
*wrong*, since every measured benefit of a tooled critic depends on
there being something to catch, and this question had nothing.

## Reading the percentages

Percentages flatter the knob here for a reason unrelated to its value:
the researcher accounts for ~89% of completion tokens in the off arm,
so anything the other four roles do is measured against a large
denominator. Report the per-role absolute numbers, not the totals'
percentage.

## Reproduce

```
python -m consultants.server --host 127.0.0.1 --port 38195 &   # current code
python -m benchmarks.consultants.role_tools_bench --tier cost \
    --project <throwaway dir with the code under discussion> \
    --base http://127.0.0.1:38195 --effort high \
    --question "..." --out <dir>
```

`--project`'s `.claude-hooks/consultants.toml` is **rewritten per arm**;
never point it at a project you care about.
