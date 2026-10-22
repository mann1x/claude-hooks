# Consultation transcript — csl-2026-10-22-1748-da6c

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read the four role system prompts in `consultants/engine/council.py:96-195` (PLANNER_SYSTEM ~:117, RESEARCHER_SYSTEM ~:127, CRITIC_SYSTEM ~:145, SYNTHESIZER_SYSTEM ~:157) and note each role's one-line mandate.
2. Confirm the same four role names in the graph wiring and node functions: `consultants/engine/graph.py` (build_council_graph) and `consultants/engine/council.py:436,481,667,715` (planner_node, researcher_node, critic_node, synthesizer_node).
3. Cross-check the role set in configuration: `consultants/config.py` (the `role.*` config sections, e.g. `config.role.critic.enabled` referenced at `consultants/engine/council.py:50`) to confirm exactly four configured roles and no extras.
4. Check for role-name references in the public docs/skills to make sure no fifth role exists: `consultants/__init__.py:3` and `docs/` / `.claude/skills/consultants--*/SKILL.md` mentions of the council topology.

---

## Researcher (round 1)

Findings (all verified this turn):

- **Role set is exactly four**, defined as a tuple in config: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; only `synthesizer` is mandatory (`MANDATORY_ROLES` — `consultants/config.py:41`, `consultants/config.py:219`).
- **planner** — "Decompose the question into 3-7 concrete investigation steps the researcher will execute… You are NOT answering the question — your output is the researcher's plan." `consultants/engine/council.py:116-124` (prompt const at `:116`).
- **researcher** — "Execute the planner's numbered plan with the available tools… Cite findings as `path:line`… Do NOT answer the user's question — that's the synthesizer's job." `consultants/engine/council.py:126-142`.
- **critic** — "Decide whether the researcher's evidence is sufficient for the synthesizer", emitting `DECISION: ready` or `DECISION: needs_more_research`, defaulting to ready. `consultants/engine/council.py:144-154`.
- **synthesizer** — "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict. Lead with the bottom line on the first line." `consultants/engine/council.py:156-168`; self-critic variant at `consultants/engine/council.py:177-193` (used when the critic node is dropped, `consultants/engine/graph.py:113-116`).
- **Node functions match the same four names**: `planner_node` `consultants/engine/council.py:436`, `researcher_node` `consultants/engine/council.py:481`, `critic_node` `consultants/engine/council.py:667`, `synthesizer_node` `consultants/engine/council.py:715`.
- **Graph wiring confirms the same four nodes**: `sg.add_node("planner", …)` `consultants/engine/graph.py:325-330`, `"researcher"` `consultants/engine/graph.py:331-333`, `"critic"` `consultants/engine/graph.py:334-335`, `"synthesizer"` `consultants/engine/graph.py:336-340`; role→client/model dispatch via wrappers `_wrap_planner`/`_wrap_researcher`/`_wrap_critic`/`_wrap_synthesizer` `consultants/engine/graph.py:134-180`.
- **Flow**: planner → researcher (optionally parallel `Send` fan-out when plan has ≥ `FANOUT_MIN_ITEMS=2` items, `consultants/engine/graph.py:342-386`, `consultants/engine/council.py:297`) → critic conditional edge back to researcher or forward to synthesizer (`consultants/engine/graph.py:406-425`, router `council.route_after_critic` `consultants/engine/council.py:366`).
- The role names are also stated verbatim in the shared preamble: "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop." `consultants/engine/council.py:96-107` (specifically `:98-99`).
- Per-role reasoning effort defaults differ (planner medium, researcher high, critic medium, synthesizer high) — `consultants/config.py:86-91`.

---

## Researcher (round 1)

- The canonical role tuple contains exactly four roles—`planner`, `researcher`, `critic`, and `synthesizer`—with no extras (`consultants/config.py:40`).
- Configuration merging and rendering iterate only that fixed `ROLES` tuple, while `role()` rejects unknown names (`consultants/config.py:210`, `consultants/config.py:264`, `consultants/config.py:121`).
- The public council skill independently describes the same topology: `planner → researcher → critic → synthesizer` (`.claude/skills/consultants/SKILL.md:3`).
- The package documentation likewise identifies the four specialists (`consultants/__init__.py:3`).

---

## Synthesizer

The council has exactly four roles — **planner** (decomposes the question into 3-7 concrete investigation steps for the researcher, never answering it itself, `consultants/engine/council.py:116-124`), **researcher** (executes that plan with the project tools and reports findings as `path:line` citations, `consultants/engine/council.py:126-142`), **critic** (judges whether the researcher's evidence suffices, emitting `DECISION: ready` or `DECISION: needs_more_research`, `consultants/engine/council.py:144-154`), and **synthesizer** (writes the final answer from the plan, report(s), and verdict, `consultants/engine/council.py:156-168`) — the set being fixed as `ROLES = ("planner", "researcher", "critic", "synthesizer")` in `consultants/config.py:40` and named in the shared preamble at `consultants/engine/council.py:98-99`.

---
