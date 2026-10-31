# Consultation transcript — csl-2026-10-27-1126-f09d

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

The consultants council's four roles are the **planner** (decomposes the question into an investigation plan), **researcher** (executes the plan with project tools), **critic** (judges whether the evidence is sufficient), and **synthesizer** (writes the final answer) — defined in `consultants/engine/council.py:98` and as the four role prompts `PLANNER_SYSTEM`, `RESEARCHER_SYSTEM`, `CRITIC_SYSTEM`, `SYNTHESIZER_SYSTEM` at `consultants/engine/council.py:116`, `:126`, `:144`, `:156`.

---

## Researcher

**Report:**

- The four roles — planner, researcher, critic, synthesizer — are named together in the council preamble at `consultants/engine/council.py:98`.
- Each has a dedicated system prompt: `PLANNER_SYSTEM` (`consultants/engine/council.py:116`), `RESEARCHER_SYSTEM` (`consultants/engine/council.py:126`), `CRITIC_SYSTEM` (`consultants/engine/council.py:144`), `SYNTHESIZER_SYSTEM` (`consultants/engine/council.py:156`), all wrapped via `_role_prompt` (`consultants/engine/council.py:110`).
- Role definitions confirmed from the actual prompt text: planner decomposes the question into investigation steps (council.py:117), researcher executes the plan with tools and cites `path:line` (council.py:127), critic decides whether evidence is sufficient (council.py:145), synthesizer writes the final answer (council.py:157).
- Note: there is also a fifth variant prompt `SYNTHESIZER_SELF_CRITIC_SYSTEM` at `consultants/engine/council.py:177` used at low/medium effort, but the council is still the four-role structure above.

---

## Synthesizer

The four roles of the consultants council are the planner, researcher, critic, and synthesizer (`consultants/engine/council.py:98`), each with its own system prompt (`PLANNER_SYSTEM` at `consultants/engine/council.py:116`, `RESEARCHER_SYSTEM` at `consultants/engine/council.py:126`, and the critic and synthesizer prompts further down in the same file).

---
