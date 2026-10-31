# Consultation transcript — csl-2026-10-22-1635-032a

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/config.py:40` to confirm the canonical role tuple `ROLES = ("planner", "researcher", "critic", "synthesizer")` and `MANDATORY_ROLES = {"synthesizer"}` (`consultants/config.py:41`).
2. Read `consultants/engine/council.py:116-166` to capture each role's one-line duty from `PLANNER_SYSTEM`, `RESEARCHER_SYSTEM`, `CRITIC_SYSTEM`, `SYNTHESIZER_SYSTEM` (decompose → execute plan with tools → judge evidence sufficiency → write final user-facing answer).
3. Cross-check `consultants/engine/graph.py:13-17` (pipeline order `planner → researcher → critic → synthesizer`, with a `needs_more` loop back to the researcher) and `consultants/engine/council.py:96-100` (`COUNCIL_PREAMBLE` naming the same four roles) to confirm the set is complete and no fifth role exists.
4. Verify no other role lists exist elsewhere (e.g. `consultants/server/app.py:57-58` progress keys, `consultants/engine/storage.py:41-43` `RoleTurn.role` docstring) before finalizing the one-sentence answer.

---

## Researcher (round 1)

Findings (all cites verified this turn):

- Canonical role tuple: `ROLES = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; only `synthesizer` is mandatory (`MANDATORY_ROLES = frozenset({"synthesizer"})`) — `consultants/config.py:41`, and it can never be disabled — `consultants/config.py:219`, `consultants/config.py:312-315`. Pipeline validity also requires at least one of planner/researcher — `consultants/config.py:376-391`.
- Same four names appear in the shared council preamble: "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop." — `consultants/engine/council.py:96-107`.
- Planner duty: "Decompose the question into 3-7 concrete investigation steps the researcher will execute with project tools… You are NOT answering the question — your output is the researcher's plan." — `consultants/engine/council.py:116-124` (node: `planner_node` — `consultants/engine/council.py:436`).
- Researcher duty: "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory). Cite findings as `path:line`." — `consultants/engine/council.py:126-142` (node: `researcher_node` — `consultants/engine/council.py:481`).
- Critic duty: "Decide whether the researcher's evidence is sufficient for the synthesizer", emitting `DECISION: ready` or `DECISION: needs_more_research` — `consultants/engine/council.py:144-154` (node: `critic_node` — `consultants/engine/council.py:667`; routing back to researcher vs. synthesizer — `consultants/engine/council.py:366-390`).
- Synthesizer duty: "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict… Do NOT mention the council, the roles, or the process" — `consultants/engine/council.py:156-168` (node: `synthesizer_node` — `consultants/engine/council.py:715`).
- Note for the synthesizer: at effort low/medium the critic node is dropped and the synthesizer self-critics instead — `consultants/engine/council.py:170-193`, caps at `consultants/engine/council.py:59-75`.

Evidence sufficient for a one-sentence answer naming all four roles with their duties.

---

## Researcher (round 1)

- The canonical role set is exactly four—`planner`, `researcher`, `critic`, and `synthesizer`—as defined by `ROLES` (`consultants/config.py:40`).
- The graph pipeline follows `planner → researcher → critic → synthesizer`, with the critic able to loop back to the researcher when more research is needed (`consultants/engine/graph.py:13`, `consultants/engine/graph.py:17`).
- The shared council preamble independently names the same four roles (`consultants/engine/council.py:96`, `consultants/engine/council.py:98`).
- Server progress keys use the same four-role set (`consultants/server/app.py:57`).
- Storage documentation likewise restricts `RoleTurn.role` to those four roles (`consultants/engine/storage.py:41`).
- No fifth council role is present; the sole variant is an optional low/medium-effort “synthesizer with self-critic” mode, not a separate role (`consultants/engine/council.py:170`, `consultants/engine/council.py:178`).

---

## Synthesizer

The consultants council has exactly four roles — `planner`, `researcher`, `critic`, `synthesizer` (`consultants/config.py:40`) — where the **planner** decomposes the question into 3–7 concrete investigation steps (`consultants/engine/council.py:117`), the **researcher** executes that plan with project tools and cites findings as `path:line` (`consultants/engine/council.py:127`), the **critic** judges whether the evidence is sufficient and returns `DECISION: ready` or `DECISION: needs_more_research` (`consultants/engine/council.py:145`), and the **synthesizer** writes the final user-facing answer from the plan, report(s), and verdict (`consultants/engine/council.py:157`).

---
