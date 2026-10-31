# Consultation transcript — csl-2026-10-22-0758-e303

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/engine/council.py:96-113` and confirm the four role names in `COUNCIL_PREAMBLE` ("planner, researcher, critic, synthesizer") and in the `PLANNER_SYSTEM` / `RESEARCHER_SYSTEM` / `CRITIC_SYSTEM` / `SYNTHESIZER_SYSTEM` prompt constants at `consultants/engine/council.py:116-168`.
2. Read the four role job descriptions verbatim — `consultants/engine/council.py:116-124` (planner decomposes question into investigation steps), `:126-142` (researcher executes plan with tools, cites `path:line`), `:144-154` (critic emits `DECISION: ready` / `needs_more_research`), `:156-168` (synthesizer writes the final user-facing answer) — so each role's one-line function is grounded in code, not inferred.
3. Check `consultants/config.py` for a per-role config block (e.g. `config.role.critic.enabled` referenced at `consultants/engine/council.py:50`) to confirm exactly four configured roles and no fifth role (e.g. no separate "advisor" or "verifier" role).
4. Verify the node implementations match the four roles one-to-one: `planner_node` (`consultants/engine/council.py:~440-470`), `researcher_node` (`:481-664`), `critic_node` (`:667-712`), `synthesizer_node` (`:715-755`) — confirm there is no fifth node in `consultants/engine/graph.py` (check the graph's node registration) and that the low/medium "self-critic" variant (`:177-194`) folds the critic duty into the synthesizer rather than adding a role.
5. Grep `consultants/` and `claude_hooks/get_advice/` for any other role labels (e.g. "advisor", "verifier", "judge") that could be confused with council roles, and note that `get_advice` is a separate feature, so its roles are excluded.
6. Assemble the final one-sentence answer naming the four roles with a one-clause description each (plan, research, critique, synthesize) and cite `consultants/engine/council.py:116`, `:126`, `:144`, `:156` for each role's grounding.

---

## Researcher (round 1)

Findings (all lines verified this turn):

- Four roles, named explicitly in the council preamble: "planner, researcher, critic, synthesizer" — `consultants/engine/council.py:96-107` (names at `:98-99`).
- Exactly four configured roles, no fifth: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; per-role config blocks are `[role.<name>]` — `consultants/config.py:264-272`; role lookup rejects anything else — `consultants/config.py:121-126`.
- Role 1 — planner: "Decompose the question into 3-7 concrete investigation steps the researcher will execute with project tools… your output is the researcher's plan" — `consultants/engine/council.py:116-124`; node impl `planner_node` — `consultants/engine/council.py:436-478`.
- Role 2 — researcher: "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory). Cite findings as `path:line`" — `consultants/engine/council.py:126-142`; node impl `researcher_node` — `consultants/engine/council.py:481-664`.
- Role 3 — critic: "Decide whether the researcher's evidence is sufficient for the synthesizer", emitting `DECISION: ready` / `DECISION: needs_more_research` — `consultants/engine/council.py:144-154`; decision regex — `consultants/engine/council.py:342-357`; node impl `critic_node` — `consultants/engine/council.py:667-712`.
- Role 4 — synthesizer: "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict" — `consultants/engine/council.py:156-168`; node impl `synthesizer_node` — `consultants/engine/council.py:715-758`.
- Graph wiring confirms exactly these four nodes: `sg.add_node("planner"...)`, `"researcher"`, `"critic"`, `"synthesizer"` — `consultants/engine/graph.py:325-340`; conditional edges `planner`→fan-out, `critic`→`route_after_critic` → researcher|synthesizer — `consultants/engine/graph.py:399-416`, `consultants/engine/council.py:366-390`.
- No fifth role exists: grep for `advisor|verifier|referee|judge|moderator` across `consultants/` returns only an unrelated comment at `consultants/engine/council.py:18` and `consultants/engine/graph.py:424` ("advisory — straight to synthesizer"). The advisor is a separate subsystem (`claude_hooks/get_advice/`), not a council role.
- Nuance worth flagging to the synthesizer: the critic node is optional — at effort=low/medium the critic is dropped and the synthesizer self-critiques instead (`consultants/engine/council.py:170-193`; budgets at `consultants/engine/council.py:59-75`; per-role enable switch in config at `consultants/config.py:210-219`), so the canonical four-role set is fixed but not always all four are executed.

---

## Researcher (round 1)

- The council defines exactly four roles—`planner`, `researcher`, `critic`, and `synthesizer`—in its canonical `ROLES` tuple (`consultants/config.py:40`), with one corresponding node implementation each (`consultants/engine/council.py:436`, `consultants/engine/council.py:481`, `consultants/engine/council.py:667`, `consultants/engine/council.py:715`).
- Graph registration adds only those four possible nodes: `planner`, `researcher`, `critic`, and mandatory `synthesizer` (`consultants/engine/graph.py:325`, `consultants/engine/graph.py:331`, `consultants/engine/graph.py:334`, `consultants/engine/graph.py:336`).
- Role duties are: planner decomposes the question into investigation steps (`consultants/engine/council.py:116`); researcher executes the plan with tools and cited findings (`consultants/engine/council.py:126`); critic judges evidence sufficiency and requests more research when needed (`consultants/engine/council.py:144`); synthesizer writes the final user-facing answer from the plan, research, and critique (`consultants/engine/council.py:156`).
- The self-critic prompt is a variant of the synthesizer, not a fifth role (`consultants/engine/council.py:177`); moreover, current low/medium execution drops the critic and leaves self-critic disabled rather than activating it (`consultants/server/runner.py:48`, `consultants/server/runner.py:58`, `consultants/server/runner.py:64`).
- `advisor` belongs to the separate `claude_hooks/get_advice/` feature and is not a council role (`claude_hooks/get_advice/cli.py:1`, `claude_hooks/get_advice/cli.py:39`); the council itself reuses only that feature’s prompt style (`consultants/engine/council.py:83`).

---

## Synthesizer

The consultants council has four roles: **planner** (decomposes the question into concrete investigation steps — `consultants/engine/council.py:117`), **researcher** (executes the plan with project tools and cites findings as `path:line` — `consultants/engine/council.py:127`), **critic** (judges whether the evidence is sufficient, emitting `DECISION: ready` / `needs_more_research` — `consultants/engine/council.py:145 [no needs_more_research at this line; module scope]`), and **synthesizer** (writes the final user-facing answer from the plan, reports, and verdict — `consultants/engine/council.py:157`).

This is the canonical set: `ROLES = ("planner", "researcher", "critic", "synthesizer")` (`consultants/config.py:40`), matching the preamble's role list (`consultants/engine/council.py:98`) and one node per role (`consultants/engine/graph.py:325`). Note the critic is only executed at effort high/max — at low/medium it is dropped and the synthesizer self-critic path is currently disabled (`consultants/server/runner.py:58`, `consultants/server/runner.py:67`).

---
