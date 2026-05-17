# M13 — live x-tier smoke (task #102)

**Date**: 2026-05-17
**Milestone**: M13 — live smoke + ops runbook + CHANGELOG (task #102)
**Verdict**: ✅ **PASS — engine fix landed, x-tier composition verified live**
**Predecessor**: M11c-5 (default-on flip), M11c-3 (#103 proper-composition refactor)

## Purpose

Validate the M11c-3 (#103) proper-composition engine refactor under a
**real cloud consultation** at `xhigh` effort — Phase 9 multi-model
researcher fanout (N×M lanes) composing with the optional
`tool_executor` role that M11c-5 flipped on by default. The skill-eval
protocol explicitly does NOT exercise the full council pipeline; that's
this milestone's job.

## What ran

| Component   | Value                                                              |
|-------------|--------------------------------------------------------------------|
| Effort      | `xhigh`                                                            |
| Planner     | `gemini-3-flash-preview:cloud`                                     |
| Researcher  | `gemini-3-flash-preview:cloud` (primary) + 2 extras                |
| Researcher extras | `gemma4:31b-cloud`, `glm-5.1:cloud`                          |
| Tool executor | `gemma4:31b-cloud`                                               |
| Critic      | `gemini-3-flash-preview:cloud`                                     |
| Synthesizer | `gemma4:31b-cloud`                                                 |
| Fixture     | `benchmarks/consultants/questions/tool_executor/fixtures/multipkg` |

The fixture is a tiny Python package (5 modules in `pkg/`, no entry
points, no technical debt) carefully shaped so that **the audit
cannot be answered from training data** — the researcher must drive
the filesystem tools through `tool_executor` and consume the results
in REPORT mode.

**Question**:

> Audit the multipkg package: (1) list the modules and describe what
> each one does, (2) identify the main entry function and trace its
> callers, (3) flag any TODO comments or technical debt. Cite file
> paths and line numbers.

## Run 1 — pre-fix (`csl-2026-05-17-2302-9c35`, regression)

The first attempt surfaced a regression introduced by M11c-3 and
made user-visible by M11c-5's default-on flip.

| Metric                            | Value |
|-----------------------------------|------:|
| Duration                          | 465.76 s |
| Researcher `llm_call`             | 49 |
| ↳ in REPORT mode                  | **2** |
| ↳ in PLAN mode                    | **47** |
| Tool-executor `node_enter`        | 74 |
| Tool calls                        | 198 |
| Critic passes                     | 3 |
| Synthesizer passes                | 2 |
| Final answer length               | 180 chars |
| Verdict                           | ❌ FAIL — empty audit |

**Final answer (run 1)**:

> Could not perform the audit because the researcher failed to
> execute the tool plans; no file lists, module contents, or
> search results were provided from the `multipkg/` directory.

### Root cause

The pre-M11c-3 wiring used an unconditional `tool_executor →
researcher` edge: every Send to `tool_executor` barriered there, and
researcher fired once with merged state (single-researcher REPORT
mode with cross-pollution). M11c-3 (#103) replaced that with the
conditional `_fanout_after_tool_executor` that emits one Send per
distinct `parent_lane_idx` so each lane reads only its own results.

The bug: **LangGraph 1.2 `Send` dispatches deliver ONLY the dict's
keys to the target node; channels not in the Send dict are absent
from the receiver's state even when a global `operator.add` reducer
is registered.** The M11c-3 `_fanout_after_tool_executor` Send dict
omitted `tool_results`, so each fanned-back researcher saw an
empty `tool_results` channel, `tool_results_for_round` returned
empty, and `researcher_node` fell back to PLAN mode instead of
consuming the results in REPORT mode. 47/49 researcher invocations
stayed in PLAN mode; the two REPORT-mode entries came from a late
single-researcher fallback path (`lane_idx=None`), producing the
partial degraded synthesis above.

The M11c-3 stubbed tests (`TestFanbackAfterToolExecutor`) couldn't
catch this — they invoke the routing function with a synthetic
dict and inspect its return Sends, but they never drive Pregel
and never observe what the receiver's state actually contains.

## Fix

A 2-file change:

- **`consultants/engine/graph.py`** — `_fanout_after_tool_executor`
  now computes a per-lane filter and includes the lane-matching
  `tool_results` in every emitted Send dict. The filter keeps the
  #103 composition guarantee: `parent_round == current_round` AND
  (`parent_lane_idx == this lane` OR `parent_lane_idx is None`
  for legacy rows). No sibling-lane results leak into any lane's
  Send.

- **`tests/test_consultants_v2_tool_executor_xtier_composition.py`**
  — new `TestFanbackSendCarriesToolResults` class (3 tests) that
  builds a minimal real `StateGraph`, drives a real Send, and
  asserts the receiver's state contains the expected
  `tool_results`. Plus a baseline test that locks the LangGraph
  contract (deliberately omits `tool_results` from a Send and
  asserts the worker sees empty) — if LangGraph ever changes
  semantics the test fails and we drop the explicit-pass workaround.
  Plus a white-box check on the production closure.

### Verification (pre-rerun)

| Env                          | Pre-fix  | Post-fix |
|------------------------------|----------|----------|
| `claude-hooks`               | 3550 ✓   | 3550 ✓   |
| `claude-hooks-consultants`   | 3629 ✓   | 3632 ✓ (+3 isolation tests) |

## Run 2 — post-fix (`csl-2026-05-17-2336-5124`, ✅ PASS)

Service restarted at 23:35:14 CEST, fresh consultation issued
against the same fixture + same question + same models.

| Metric                            | Value | Δ vs run 1 |
|-----------------------------------|------:|-----------:|
| Duration                          | 218.30 s | **−53%** (2.1× faster) |
| Researcher `llm_call`             | 43 |  −6 |
| ↳ in REPORT mode                  | **29** | +27 |
| ↳ in PLAN mode                    | **9** (one per lane, round 1) | −38 |
| Tool-executor `node_enter`        | 34 | −40 |
| Tool calls                        | 77 | −121 |
| Critic passes                     | 1 (no reroute) | −2 |
| Synthesizer passes                | 1 | −1 |
| Final answer length               | 756 chars | +576 |
| Cross-pollution                   | **none — 9/9 lanes PASS** |
| Verdict                           | ✅ PASS |

The researchers now report on round 1 instead of looping in PLAN
mode indefinitely. Each lane's REPORT-mode appendix contains only
its own filtered `tool_results`. The single critic pass is happy
with the synthesized findings; no rerouting needed.

### Final answer (run 2)

```
* **Modules**
    * `pkg/__init__.py`: Package metadata and versioning (`pkg/__init__.py:11`).
    * `pkg/parser.py`: DSL tokenizer and parser, providing `tokenize` (`pkg/parser.py:11`) and `parse` (`pkg/parser.py:16`) functions.
    * `pkg/runner.py`: Pipeline orchestration (`pkg/runner.py:1-3`).
    * `pkg/serializer.py`: AST to UTF-8 bytes conversion (`pkg/serializer.py:10`).
    * `pkg/validator.py`: AST validation for `name` and `version` keys (`pkg/validator.py:11-14`).

* **Main Entry Function**
    * Function: `pkg/runner.run` (`pkg/runner.py:14`).
    * Callers: No internal callers identified; it serves as the top-level API.

* **Technical Debt**
    * No `TODO`, `FIXME`, `XXX`, or `HACK` comments were identified in the `pkg/` directory.
```

All five modules correctly identified with line-number citations.
Main entry function `pkg/runner.run:14` correctly identified, no
callers (correct — there are none in the fixture). No technical
debt found (correct — the fixture has none).

### Cross-pollution check (M11c-3 headline contract)

The post-hoc inspector (`/tmp/m13_inspect.py`) walks the transcript
and for each researcher lane:

1. Pulls the PLAN-mode emitted intents (the lane's own `tool_plan`).
2. Pulls the REPORT-mode received prompt appendix (the
   `PRIOR TOOL RESULTS` block).
3. Asserts no intent from any sibling lane appears in this lane's
   REPORT appendix.

All 9 lanes report `✅ PASS — no sibling intents detected`.

## What M13 actually validated

1. **#103 cross-pollution prevention end-to-end** — every researcher
   lane in REPORT mode reads only its own tool_results, verified by
   substring-matching every sibling lane's PLAN-mode intents against
   every other lane's REPORT-mode appendix. Zero hits.

2. **Phase 9 multi-model researcher fanout still works at scale** —
   9 distinct researcher lanes (3 plan items × 3 models) all completed
   PLAN + REPORT cycles, all produced meaningful citations.

3. **`gemini-3-flash-preview:cloud` is a viable planner/critic** in
   the x-tier topology. Single critic pass approved the merged
   research without reroute.

4. **`gemma4:31b-cloud` as tool_executor** delivers the file listings
   + greps the researcher lanes plan. 77 tool calls across 34
   tool_executor invocations completed without error.

5. **The default-on `tool_executor` role (M11c-5) produces sound
   output under real cloud conditions** at `xhigh` effort. The flip
   stays in place.

## What M13 didn't validate

- **Higher-tier effort modes** (`xmax`, `xauto`) — not exercised.
  M11c-5's default-on covers all tiers; the fix-up path
  `_fanout_after_tool_executor` is the same closure regardless of
  effort, so the wiring is shared. xhigh exercises the same lane
  count cap as xmax in practice (3 plan items + 3 researcher models =
  9 lanes is at `FANOUT_MAX_LANES` for both tiers).

- **Critic-reroute cycles** — this run had a clean single critic pass.
  A future M13.x could craft a fixture that forces a critic reroute
  to exercise round-2 researcher behavior, but the unit test
  `test_two_researcher_lanes_three_items_each_no_pollution` already
  covers the round-trip identity-tuple math.

- **Per-lane failure recovery** — no lane failed in this run. The
  M11c-3 tombstone path (researcher lane failed exception) wasn't
  hit. The unit test `test_lane_failure_tombstone_preserves_parent_lane_idx`
  covers the data plumbing.

## Artifacts

- Pre-fix transcript: `benchmarks/consultants/questions/tool_executor/fixtures/multipkg/.claude-hooks/consultants/csl-2026-05-17-2302-9c35/transcript.db`
- Post-fix transcript: `benchmarks/consultants/questions/tool_executor/fixtures/multipkg/.claude-hooks/consultants/csl-2026-05-17-2336-5124/transcript.db`
- Inspector script: `tests/m13_inspect.py` (copied from `/tmp/`)
- Engine fix: `consultants/engine/graph.py` `_fanout_after_tool_executor`
- Integration tests: `tests/test_consultants_v2_tool_executor_xtier_composition.py::TestFanbackSendCarriesToolResults`

## Closing

Task #102 (M13) → completed.
Task #156 (M13 live x-tier smoke + result write-up + commit) → completed.
Tasks #157–#160 (M13 fix-up sequence) → all completed.

The M11c sequence is now end-to-end live-validated under real cloud
conditions. The tool_executor role is sound for default-on under
x-tier Phase 9 fanout. M14 (#104 — per-namespace TTL + distillation)
is the next milestone.
