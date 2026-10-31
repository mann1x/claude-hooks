# Consultation transcript — csl-2026-10-22-1814-94cc

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/config.py:40` (`ROLES = ("planner", "researcher", "critic", "synthesizer")`) and `consultants/config.py:41` to confirm the canonical four-role tuple and that only the synthesizer is mandatory — this tuple is the answer's core.
2. Cross-check the same four names in `consultants/engine/council.py:98-99` (`COUNCIL_PREAMBLE` lists "planner, researcher, critic, synthesizer") to confirm these are the council's four roles and not merely config keys.
3. Read each role's system prompt to capture its one-line function: `PLANNER_SYSTEM` (`consultants/engine/council.py:116`), `RESEARCHER_SYSTEM` (`:126`), `CRITIC_SYSTEM` (`:144`), `SYNTHESIZER_SYSTEM` (`:156`).
4. Verify the pipeline order/execution topology in `consultants/engine/graph.py` and `route_after_critic` (`consultants/engine/council.py:366`) so the answer states planner→researcher→critic→synthesizer correctly.
5. Note the two opt-in extra roles (`tool_executor`, `coder`) are not part of the four and check `consultants/config.py` ROLES to confirm they are excluded from the canonical set.

---

## Researcher (round 1)

**Findings — consultants council roles**

- Canonical four-role tuple: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`.
- Only the synthesizer is mandatory: `MANDATORY_ROLES: frozenset[str] = frozenset({"synthesizer"})` — `consultants/config.py:41`; enforced by re-enabling it in `_merge_layer` (`consultants/config.py:219`) and by `validate_pipeline` requiring synthesizer plus at least one of planner/researcher (`consultants/config.py:386-390`).
- Same four names confirmed as the council's roles (not mere config keys) in the shared preamble: "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop" — `consultants/engine/council.py:98-99`.
- Pipeline topology: `START -> [planner] -> [researcher] -> [critic] -> ?` → ready → `[synthesizer] -> END`; needs_more → back to `[researcher]` — `consultants/engine/graph.py:13-17`; disabled roles are skipped as nodes, synthesizer always present — `consultants/engine/graph.py:19-20`.
- Role functions, one line each, from the system prompts:
  - planner — "Decompose the question into 3-7 concrete investigation steps the researcher will execute" — `consultants/engine/council.py:116-124`.
  - researcher — "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory)" — `consultants/engine/council.py:126-142`.
  - critic — "Decide whether the researcher's evidence is sufficient for the synthesizer" (emits `DECISION: ready` / `DECISION: needs_more_research`) — `consultants/engine/council.py:144-154`.
  - synthesizer — "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict" — `consultants/engine/council.py:156-168`.
- Note for synthesizer: it also has a self-critic variant used when the critic node is dropped at effort=low/medium (`SYNTHESIZER_SELF_CRITIC_SYSTEM`, `consultants/engine/council.py:177`), and the synthesizer is explicitly told not to mention the council/roles/process to the user (`consultants/engine/council.py:164-165`).
- Per-role reasoning-effort defaults also keyed to the same four names: `DEFAULT_THINK_BY_ROLE` (planner medium, researcher high, critic medium, synthesizer high) — `consultants/config.py:86-91`.

Evidence sufficient for the answer; no unverified citations above.

---

## Researcher (round 1)

- Canonical council roles are exactly `planner`, `researcher`, `critic`, and `synthesizer` in pipeline order (`consultants/config.py:40`).
- The graph documents the same order—planner → researcher → critic → synthesizer—with an optional critic-triggered loop back to researcher (`consultants/engine/graph.py:13`, `consultants/engine/graph.py:17`).
- `route_after_critic` returns either `researcher` for more work or `synthesizer` when ready/capped (`consultants/engine/council.py:366`, `consultants/engine/council.py:372`, `consultants/engine/council.py:390`).
- Role prompts independently identify planner as plan decomposer, researcher as plan executor, critic as evidence sufficiency judge, and synthesizer as final-answer writer (`consultants/engine/council.py:116`, `consultants/engine/council.py:126`, `consultants/engine/council.py:144`, `consultants/engine/council.py:156`).
- `tool_executor` is a callable dependency supplied to the researcher, not a council role (`consultants/engine/graph.py:109`, `consultants/engine/graph.py:150`); no code evidence establishes `coder` as a role, and neither appears in the canonical `ROLES` tuple (`consultants/config.py:40`).

---

## Synthesizer

The council's four roles are the **planner**, who decomposes the question into 3-7 concrete investigation steps (`consultants/engine/council.py:116`); the **researcher**, who executes that plan with the project's file/search tools and reports findings with `path:line` citations (`consultants/engine/council.py:126`); the **critic**, who judges whether the researcher's evidence is sufficient and returns `DECISION: ready` or `DECISION: needs_more_research` (`consultants/engine/council.py:144`); and the **synthesizer**, who writes the final user-facing answer from the plan, report(s), and verdict (`consultants/engine/council.py:156`).

The canonical set is `ROLES = ("planner", "researcher", "critic", "synthesizer")` (`consultants/config.py:40`), with only the synthesizer mandatory (`consultants/config.py:41`, `consultants/engine/graph.py:20`); the pipeline runs planner → researcher → critic → synthesizer, with the critic able to loop back to the researcher for another round (`consultants/engine/graph.py:13`, `consultants/engine/graph.py:17`).

---
