# Consultation transcript — csl-2026-09-23-2041-e86f

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read the LangGraph pipeline diagram at `consultants/engine/graph.py:13-20`, which lists the roles and their edges (planner → researcher → critic → synthesizer).
2. Verify the four role definitions in `consultants/engine/council.py` — `PLANNER_SYSTEM` (council.py:116), `RESEARCHER_SYSTEM` (council.py:126), `CRITIC_SYSTEM` (council.py:144), `SYNTHESIZER_SYSTEM` (council.py:156) — and the preamble at council.py:98 naming all four.
3. Confirm the canonical `ROLES` tuple in `consultants/config.py:37` (imported by graph.py:37) matches those four names.
4. Note the optionality rules from graph.py:197-202 and council.py:48-50 (synthesizer mandatory, critic budget-dependent) so the sentence can be phrased accurately.

---

## Researcher (round 1)

**Findings:**

- Pipeline topology confirmed at `consultants/engine/graph.py:13-15`: `START -> [planner] -> [researcher] -> [critic] -> [synthesizer] -> END` (with a critic→researcher loop on `needs_more`, `consultants/engine/graph.py:17`).
- All four role prompts verified at `consultants/engine/council.py:116` (PLANNER_SYSTEM), `council.py:126` (RESEARCHER_SYSTEM), `council.py:144` (CRITIC_SYSTEM), `council.py:156` (SYNTHESIZER_SYSTEM); the preamble naming all four is at `consultants/engine/council.py:96-99`.

**Answer (one sentence):** The four roles are the planner (decomposes the question into investigation steps), the researcher (executes the plan with tools and reports `path:line` findings), the critic (decides whether the evidence is ready or more research is needed), and the synthesizer (writes the final user-facing answer) — per `consultants/engine/graph.py:13` and the role prompts in `consultants/engine/council.py:116,126,144,156`.

---

## Researcher (round 1)

Findings:

- Canonical `ROLES` tuple: `consultants/config.py:40` — `("planner", "researcher", "critic", "synthesizer")`; `MANDATORY_ROLES = frozenset({"synthesizer"})` at `consultants/config.py:41`.
- Planner's cited line number was stale: `ROLES` is at config.py:40, not :37 (import in `consultants/engine/graph.py:37` confirmed).
- Synthesizer mandatory: `consultants/engine/graph.py:197-198` (`if "synthesizer" not in enabled_set: raise ValueError("synthesizer is mandatory")`); pipeline order of remaining roles at `consultants/engine/graph.py:202-203`.
- Critic is budget-dependent: `consultants/engine/council.py:48-50` — effort tier maps to `(researcher_rounds_max, critic_reroutes_max, critic_enabled_when_optional)`; at low effort the path is "planner → researcher → synthesizer (no critic)" (`consultants/engine/council.py:67`); at low/medium a self-critic variant runs instead (`consultants/engine/council.py:170-175`), with the dedicated critic retained at high effort.

Answer sentence (grounded): The consultants council has four roles — **planner, researcher, critic, and synthesizer** — with the synthesizer always mandatory and the critic optional depending on effort budget (`consultants/config.py:40-41`, `consultants/engine/council.py:48-50`).

---

## Synthesizer

The four roles of the consultants council are **planner, researcher, critic, and synthesizer** (`consultants/config.py:40`), connected in a pipeline where the planner decomposes the question, the researcher gathers evidence, the critic decides whether it's ready or more research is needed, and the synthesizer writes the final answer (`consultants/engine/graph.py:13-17`).

---
