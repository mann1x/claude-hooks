---
suite: role_tools
suite_version: "1.3"
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
  silent_fix_ceiling: 0
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
| `silent_fix_ceiling` | 0 | A silent fix is worse than a miss and must not be traded against recall. |

## Silent correction (v1.3)

The v1.2 run caught the tooled critic doing something a recall number
cannot express. On `hard-02` it fetched the right line, wrote *"…and
`should_retry` is defined at `retry.py:14`"*, and then called the
report accurate — the report had cited `retry.py:1`. It looked, it got
the right answer, and it kept it to itself. The synthesizer went on
relaying the wrong cite.

That is strictly worse than not looking: the evidence was in hand and
thrown away, and the council paid for the tool call. It also breaks
attribution — nobody downstream can tell which researcher was wrong.

`## CORRECTION_TOKENS` names the *correct* fact for questions whose
falsehood is a wrong value or a wrong line (`15` where the research
said `5`; `retry.py:14` where it said `:1`). Presence in a verdict is
evidence the critic looked and got it right. A trial counts as a
**silent fix** when a correction token is present, the planted
falsehood was NOT flagged, and no `CORRECTIONS:` block was emitted.

Questions whose falsehood is pure non-existence (`retry_state.py`,
`reset_breaker`, `half_open`) have no correction token — there is no
right value to name, so the failure mode does not apply.

The contract the critic is asked to follow:

```
CORRECTIONS:
- report N: claimed <X> — actual <Y> (`path:line`)
```

Attributed by the `RESEARCHER REPORT (round N)` header, which the
critic and the synthesizer both see under the same label. Emitting
`ready` *with* a corrections block is the intended cheap outcome — a
correction is not grounds for another research round, since the fact
is already resolved.

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
