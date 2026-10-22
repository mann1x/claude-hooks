# Consultation transcript — csl-2026-10-22-2049-1cba

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Confirm the four role names and order in `consultants/config.py:40` (`planner`, `researcher`, `critic`, `synthesizer`).
2. Verify each role’s responsibility from the prompt definitions in `consultants/engine/council.py:116-168`.
3. Check the council pipeline order in `consultants/engine/graph.py:13-17` and use the role names in one short sentence, without describing their duties.

---

## Researcher (round 1)

Findings (evidence-grounded):

- **Four roles, canonical order** — `consultants/config.py:40`: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")`. Pipeline order is enforced elsewhere too: `consultants/engine/graph.py:202` builds `pipeline = [r for r in ("planner", "researcher", "critic") ...]`, and `consultants/config.py:373` (`enabled_roles`) returns roles "in canonical pipeline order".
- **planner** — `consultants/engine/council.py:116-123`: decomposes the question into 3-7 concrete investigation steps for the researcher, pointing at WHERE to look (file/path/symbol); outputs a numbered list only, explicitly not answering the question.
- **researcher** — `consultants/engine/council.py:126-141`: executes the planner's numbered plan with project tools (read_file, grep, glob, list_files, survey_project, recall_memory), citing findings as `path:line`, batching tool calls, and reporting without answering the user question.
- **critic** — `consultants/engine/council.py:144-153`: decides whether the researcher's evidence is sufficient for the synthesizer, emitting `DECISION: ready` or `DECISION: needs_more_research`; defaults to ready unless a concrete missing fact is named. Routing back to the researcher on `needs_more_research` is at `consultants/engine/council.py:366` (`route_after_critic`) and `consultants/engine/graph.py:412-419`.
- **synthesizer** — `consultants/engine/council.py:156-167`: writes the final user-facing answer, consuming the planner's plan, the researcher's report(s), and the critic's verdict, leading with the bottom line and citing `path:line`. Mandatory role: `consultants/config.py:41` (`MANDATORY_ROLES = frozenset({"synthesizer"})`) and `consultants/config.py:219`.
- **Graph topology** — `consultants/engine/graph.py:13-17`: `START -> [planner] -> [researcher] -> [critic] -> {ready → [synthesizer] → END | needs_more → [researcher] loop}`.
- **Variance** — at effort=low/medium the critic node is dropped and the synthesizer self-critiques internally (`consultants/engine/council.py:170-177`, `SYNTHESIZER_SELF_CRITIC_SYSTEM` at `council.py:177`).

---

## Researcher (round 1)

**Findings (sub-research lane 2)**

- Pipeline order confirmed in the module docstring: `START -> [planner] -> [researcher] -> [critic] -> ?` with `ready -> [synthesizer] -> END` and `needs_more -> [researcher]` loop — `consultants/engine/graph.py:13`, `consultants/engine/graph.py:15`, `consultants/engine/graph.py:17`.
- Authoritative role tuple: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; `MANDATORY_ROLES = {"synthesizer"}` at `consultants/config.py:41`.
- Per-role prompts confirm one role each: `PLANNER_SYSTEM` `consultants/engine/council.py:116`, `RESEARCHER_SYSTEM` `consultants/engine/council.py:126`, `CRITIC_SYSTEM` `consultants/engine/council.py:144`, `SYNTHESIZER_SYSTEM` `consultants/engine/council.py:156`.
- Node wrappers map 1:1 to those roles: `_wrap_planner` `consultants/engine/graph.py:134`, `_wrap_researcher` `consultants/engine/graph.py:145`, `_wrap_critic` `consultants/engine/graph.py:160`, `_wrap_synthesizer` `consultants/engine/graph.py:171`.
- Same four names restated in the shared preamble: "The other roles (planner, researcher, critic, synthesizer)" — `consultants/engine/council.py:98`; and in the package docstring `consultants/__init__.py:4`.
- Edge behavior: critic is dropped at low/medium effort (`consultants/server/runner.py:58`), and the synthesizer may absorb critic duty self-critically (`consultants/engine/graph.py:171-179`, `SYNTHESIZER_SELF_CRITIC_SYSTEM` `consultants/engine/council.py:177`) — relevant caveat: the *default* set is still all four roles, but a run may execute only three.

**Ready-to-use one-liner:** The consultants council has four roles — planner, researcher, critic, and synthesizer (`consultants/config.py:40`; pipeline order at `consultants/engine/graph.py:13-17`).

---

## Synthesizer

The consultants council has four roles: planner, researcher, critic, and synthesizer (`consultants/config.py:40`), run in that pipeline order (`consultants/engine/graph.py:13-17`).

---
