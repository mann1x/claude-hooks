# Consultation transcript — csl-2026-10-22-0823-d9de

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/__init__.py:1-6` and confirm the council’s four named specialist roles are planner, researcher, critic, and synthesizer.
2. Read `consultants/engine/council.py:96-107` to verify the same four-role roster appears in the council preamble.
3. Read `consultants/engine/council.py:116-168` to verify each role’s distinct function and report that one short sentence with `path:line` support.

---

## Researcher (round 1)

Findings (plan lane 1, both items verified):

- `consultants/__init__.py:3-4` — package docstring: "runs a council of specialist agents (planner / researcher / critic / synthesizer) over the same Ollama proxy claude-hooks already uses for HyDE, Caliber, and `/get-advice`."
- `consultants/config.py:40` — canonical roster constant: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")`; `consultants/config.py:41` marks `synthesizer` as the only mandatory role; `consultants/config.py:382-390` requires at least one of planner/researcher.
- `consultants/engine/council.py:96-107` — `COUNCIL_PREAMBLE` names the same four: "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop."
- Role prompts: `PLANNER_SYSTEM` `consultants/engine/council.py:116-124` (decompose into 3-7 steps for the researcher), `RESEARCHER_SYSTEM` `consultants/engine/council.py:126` (executes plan with tools, cites `path:line`), `CRITIC_SYSTEM` `consultants/engine/council.py:144-154` (verdict `ready` / `needs_more_research`), `SYNTHESIZER_SYSTEM` `consultants/engine/council.py:156-168` (writes the user-facing final answer).
- Node implementations: `planner_node` `consultants/engine/council.py:436`, `researcher_node` `consultants/engine/council.py:481`, `critic_node` `consultants/engine/council.py:667`, `synthesizer_node` `consultants/engine/council.py:715`.
- LangGraph topology comment `consultants/engine/graph.py:13-17`: `START -> [planner] -> [researcher] -> [critic] -> ?` with `ready -> [synthesizer] -> END` and `needs_more -> [researcher]` loop; edge builder `consultants/engine/graph.py:202-220`; critic conditional edge `consultants/engine/graph.py:412-425` with route constants `ROUTE_SYNTHESIZER`/`ROUTE_RESEARCHER` at `consultants/engine/council.py:362-363`.
- Per-role reasoning-effort defaults also list exactly the four roles: `consultants/config.py:86-91`.
- Note: at effort < high the critic node is dropped and the synthesizer self-critics instead (`consultants/engine/council.py:170-180`, `consultants/engine/graph.py:113-116`).

Evidence sufficient for the answer sentence: the four roles are planner, researcher, critic, synthesizer, each with the one-line duty above.

---

## Researcher (round 1)

Findings (all from `consultants/engine/council.py`, read in full):

- **Planner** — decomposes the question into 3–7 concrete investigation steps (file/path/symbol pointers) for the researcher; explicitly does not answer the question. `consultants/engine/council.py:116`
- **Researcher** — executes the planner's numbered plan with project tools (read_file, grep, glob, list_files, survey_project, recall_memory) and reports findings as `path:line` bullets; does not answer the user. `consultants/engine/council.py:126`
- **Critic** — judges whether the researcher's evidence is sufficient, emitting `DECISION: ready` or `DECISION: needs_more_research` (which re-routes to the researcher). `consultants/engine/council.py:144`; routing logic at `consultants/engine/council.py:366`
- **Synthesizer** — writes the final user-facing answer from plan + research reports + critic verdict, leading with the bottom line and citing `path:line`. `consultants/engine/council.py:156`

Supporting: the four role names are also enumerated in the shared preamble (`consultants/engine/council.py:98`), and each role has a node implementation (`planner_node:436`, `researcher_node:481`, `critic_node:667`, `synthesizer_node:715`) in the same file. At effort=low/medium the critic node is dropped and the synthesizer self-critiques instead (`consultants/engine/council.py:177`, caps at `consultants/engine/council.py:59`).

One-sentence answer supported by the above: the council's four roles are the **planner** (decomposes the question into a research plan), the **researcher** (executes that plan with project tools and reports `path:line` findings), the **critic** (verdict on whether evidence suffices, sending it back for more research if not), and the **synthesizer** (writes the final answer).

---

## Synthesizer

The council's four roles are the **planner** (decomposes the question into a 3–7 step investigation plan, `consultants/engine/council.py:116`), the **researcher** (executes that plan with project tools and reports findings as `path:line` citations, `consultants/engine/council.py:126`), the **critic** (verdict on whether the evidence suffices — `DECISION: ready` or `DECISION: needs_more_research`, `consultants/engine/council.py:144`), and the **synthesizer** (writes the final user-facing answer from the plan, reports, and verdict, `consultants/engine/council.py:156`) — the same four names enumerated in the shared preamble (`consultants/engine/council.py:98`) and the package docstring (`consultants/__init__.py:3-4`).

---
