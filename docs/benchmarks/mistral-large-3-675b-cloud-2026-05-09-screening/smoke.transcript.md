# Consultation transcript — csl-2026-05-09-0633-f416

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Inspect `src/council/roles.py` for class definitions or docstrings describing consultant roles.
2. Search `src/council/` for enum or constant assignments (e.g., `ROLE_*`) that label the four roles.
3. Check `docs/architecture.md` or `README.md` for a section titled "Council Roles" or similar.
4. Review `src/council/__init__.py` for exported role names or a roles registry.
5. Scan `src/council/prompts/` for template files or comments referencing role-specific behavior.
6. Examine `tests/council/test_roles.py` for test cases that instantiate or validate the four roles.

---

## Researcher (round 1)

grep Republike{"pattern": "role|Role|ROLE_", "path": "consultants/engine/council.py"}}

---

## Researcher (round 1)

- The four roles of the consultants council are explicitly named as **planner, researcher, critic, synthesizer** in the benchmark query baseline: `docs/consultants-benchmarks.md:41`.
- The same four roles appear as the canonical set in council pipeline documentation: `consultants/engine/council.py:98-99`.
- Role definitions and pipeline wiring are implemented in `consultants/engine/council.py` (e.g., `planner_node`, `researcher_node`, `critic_node`, `synthesizer_node` functions).
- LangGraph wiring for the roles is in `consultants/engine/graph.py:13` (START → [planner] → [researcher] → [critic] → ? → [synthesizer] → END).
- Role names are used as keys in state tracking (`state["retries_by_role"]`, `state["_role_failed"]`): `consultants/engine/graph.py:72`, `consultants/engine/graph.py:75`.
- Role names are exported in the storage schema (`RoleTurn.role` field): `consultants/engine/storage.py:41-44`.
- Role names are used in trace instrumentation (`event: "node_enter", "role": "planner"`): `consultants/engine/trace.py:95`, `consultants/engine/trace.py:117`.
- The smoke benchmark explicitly tests that the council names all four roles: `docs/consultants-benchmarks.md:35` (pass criteria: "answer names all four roles").

---

## Researcher (round 1)

**Report:**
- The consultants council consists of four roles: **planner**, **researcher**, **critic**, and **synthesizer**, as defined in the system prompts and node implementations in `consultants/engine/council.py:98-193`.
- Each role is explicitly named and described in its own system prompt (`PLANNER_SYSTEM`, `RESEARCHER_SYSTEM`, `CRITIC_SYSTEM`, `SYNTHESIZER_SYSTEM`) at `consultants/engine/council.py:116-168`.
- The roles are instantiated as nodes (`planner_node`, `researcher_node`, `critic_node`, `synthesizer_node`) in the same file (`consultants/engine/council.py:436-758`).
- The critic role is conditionally enabled based on effort level, as seen in `consultants/server/runner.py:48-67`.

---

## Synthesizer

The four roles of the consultants council are **planner, researcher, critic, and synthesizer**, as defined in `consultants/engine/council.py:98-99` and implemented throughout the pipeline.

---
