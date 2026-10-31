# Consultation transcript — csl-2026-10-22-1700-0ccd

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Verify the canonical council role set in `consultants/config.py:40-41`, where `ROLES` defines planner, researcher, critic, and synthesizer.
2. Read each role’s mandate in `consultants/engine/council.py:116-159`, covering planning, research, evidence review, and final synthesis.
3. Confirm the roles’ execution order and interactions in `consultants/engine/graph.py:13-17`, then return their names in one short sentence.

---

## Researcher (round 1)

Findings (plan lane 1, both steps verified):

- Canonical role set confirmed: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40`; `MANDATORY_ROLES = frozenset({"synthesizer"})` at `consultants/config.py:41` (synthesizer can never be disabled — enforced at `consultants/config.py:219`).
- Planner mandate: "Decompose the question into 3-7 concrete investigation steps... Each step is one sentence pointing at WHERE to look (file/path/symbol) or WHAT to verify... You are NOT answering the question — your output is the researcher's plan." Output is a numbered list only. `consultants/engine/council.py:116-124`.
- Researcher mandate: "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory). Cite findings as `path:line`." Explicitly does NOT answer the user question — "that's the synthesizer's job." `consultants/engine/council.py:126-142`.
- Critic mandate: "Decide whether the researcher's evidence is sufficient for the synthesizer." Emits `DECISION: ready` or `DECISION: needs_more_research`; defaults to ready unless a concrete missing fact can be named. `consultants/engine/council.py:144-154`.
- Synthesizer mandate: "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict. Lead with the bottom line on the first line." Must not mention the council/roles/process. `consultants/engine/council.py:156-168`.
- Pipeline-order confirmation: `enabled_roles()` returns roles "in canonical pipeline order" from `ROLES` (`consultants/config.py:371-373`); validation requires synthesizer enabled plus at least one of planner/researcher (`consultants/config.py:376-391`).
- Routing corroborates the four-stage flow: planner → researcher (fanned-out lanes, `FANOUT_MAX_LANES = 3` at `consultants/engine/council.py:307`) → critic → synthesizer, with `route_after_critic` looping back to researcher on `needs_more_research` (`consultants/engine/council.py:366-390`).
- Effort budget mapping couples the roles: `EFFORT_CAPS` holds `researcher_rounds_max` and `critic_reroutes_max` per effort tier; at `low` the critic node is dropped in favor of `SYNTHESIZER_SELF_CRITIC_SYSTEM` (`consultants/engine/council.py:59-75`, `consultants/engine/council.py:170-193`).

One-sentence candidate grounded in the above: the planner decomposes the question into tool-directed steps, the researcher executes that plan with file tools and cites `path:line` evidence, the critic judges whether that evidence suffices (ready vs. needs_more_research), and the synthesizer writes the final cited answer for the user.

---

## Researcher (round 1)

**Findings**

- The council has exactly four roles, defined as a tuple in `consultants/config.py:40`: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")`; only `synthesizer` is mandatory (`consultants/config.py:41`).
- Execution order confirmed in the module docstring topology: `START -> [planner] -> [researcher] -> [critic] -> ?` with `ready -> [synthesizer] -> END` and `needs_more -> [researcher]` loop (`consultants/engine/graph.py:13-17`).
- Same order enforced programmatically by `plan_topology()`, which walks `("planner", "researcher", "critic")` then appends `synthesizer -> END` (`consultants/engine/graph.py:202-220`).
- Per-role duties:
  - planner: "Decompose the question into 3-7 concrete investigation steps the researcher will execute" — outputs a numbered list, does not answer (`consultants/engine/council.py:116-124`).
  - researcher: "Execute the planner's numbered plan with the available tools … Cite findings as `path:line`" (`consultants/engine/council.py:126-141`).
  - critic: "Decide whether the researcher's evidence is sufficient for the synthesizer", emitting `DECISION: ready` or `DECISION: needs_more_research` (`consultants/engine/council.py:144-154`).
  - synthesizer: "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict" (`consultants/engine/council.py:156-168`).
- Node wiring confirms one graph node per role: `planner`, `researcher`, `critic`, `synthesizer` added conditionally on `enabled`, synthesizer always (`consultants/engine/graph.py:325-340`).
- Critic→researcher re-route is a single-researcher path (no Send fan-out), gated by effort caps `critic_reroutes_max` / `researcher_rounds_max` (`consultants/engine/graph.py:406-425`, `consultants/engine/council.py:366-390`).

**Answer material (for synthesizer)**: The four roles are the planner (decomposes the question into an investigation plan), the researcher (executes that plan with tools and cites `path:line` evidence), the critic (verdicts on evidence sufficiency and can send the council back to the researcher), and the synthesizer (writes the final user-facing answer).

---

## Synthesizer

The consultants council has four roles — **planner** (decomposes the question into 3–7 tool-directed investigation steps, `consultants/engine/council.py:116-124`), **researcher** (executes that plan with the project tools and cites findings as `path:line`, `consultants/engine/council.py:126-142`), **critic** (rules whether the evidence is sufficient, emitting `DECISION: ready` or `DECISION: needs_more_research`, `consultants/engine/council.py:144-154`), and **synthesizer** (writes the final user-facing answer, `consultants/engine/council.py:156-168`).

The set is canonical in `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40`, with only `synthesizer` mandatory (`consultants/config.py:41`), and the pipeline order is planner → researcher → critic → synthesizer, looping back to the researcher on `needs_more` (`consultants/engine/graph.py:13-17`).

---
