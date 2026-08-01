---
suite: role_tools
suite_version: "1.0"
released: 2026-08-01
manifest:
  - easy-01-fabricated-file
  - easy-02-wrong-constant
  - medium-01-nonexistent-symbol
  - medium-02-all-true
  - hard-01-mixed
  - hard-02-line-drift
rubric:
  recall_floor: 0.70
  precision_floor: 0.80
  min_delta_over_untooled: 0.30
  tie_breaker: completion_tokens
---

# Role-Tools Detection Suite v1.0

Manifest for the **M-B** bench (`benchmarks/consultants/role_tools_bench.py`),
which measures what `[tools] all_roles` actually buys.

## What this suite measures

Whether a critic that *can look* catches claims a critic that cannot
must take on faith. Each question hands the critic a block of research
prose about a small fixture package and asks for a verdict. Some claims
in that prose are true of the fixture; some are fabricated. We planted
both, so the oracle needs no judge.

The arm under test is the tool surface, not the model: the same model,
the same prose, the same prompt — once with `tool_specs`/`tool_executor`
supplied and once without.

## Why precision is scored, not just recall

A critic that flags everything scores 100% recall and is useless: its
verdicts stop carrying information, the synthesizer learns to discount
them, and the council pays tool-call tokens for noise. `medium-02` is
the control — every claim in it is true of the fixture, so a flag there
is unambiguously a false positive. Any candidate that clears the recall
floor while failing the precision floor is a **fail**.

## Rubric

| threshold | value | why |
|---|---|---|
| `recall_floor` | 0.70 | Below this the tooled critic misses more planted falsehoods than it catches at the hard tier, and the CitationLinter already covers the easy ones. |
| `precision_floor` | 0.80 | One false alarm in five is the most a synthesizer can absorb before it starts ignoring the critic. |
| `min_delta_over_untooled` | 0.30 | The floor that matters. An untooled critic already catches some fabrications by prior knowledge; only the *delta* is what the tokens buy. `tool_executor` passed its absolute floors and still lost the live A/B. |

`tie_breaker: completion_tokens` — when two configurations land inside
noise of each other on recall, the cheaper one wins. This is the same
tie-breaker the coder suite uses and it exists because the cost arm is
the one that flipped `tool_executor` back off.

## Fixtures

`fixtures/retry/` is a two-module package (`retry.py`, `forwarder.py`)
modelled on the v1.13 proxy retry layer — small enough that a critic can
verify a claim in one or two tool calls, real enough that the constants
and symbol names are not guessable. Questions reference it via
`fixtures_subdir` and the bench runs the critic with that directory as
cwd, so `grep`/`read_file` resolve against the fixture and nothing else.

## When to re-run

Per the Skill-Eval Protocol: on a suite_version bump, on a change to
`_role_turn`'s caps or fallback behaviour, and when a candidate model
for a council role changes. `suite_hash` (computed by
`harness._hash_for_suite`) catches undeclared question edits.

## Known limitation

The oracle is keyword-based — a catch is scored when the verdict names
the fabricated token near a doubt word. A critic that describes the
problem without naming it scores as a miss, so **recall is a lower
bound**. `hard-02` (a correct symbol cited at a wrong line) sits at the
edge of what this can score; treat its result as directional and read
the transcript.
