# Consultation transcript — csl-2026-10-22-1719-8908

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Confirm the canonical role tuple in `consultants/config.py:40` (`ROLES = ("planner", "researcher", "critic", "synthesizer")`) and that `MANDATORY_ROLES` is `{"synthesizer"}` at `consultants/config.py:41`.
2. Read each role's system prompt in `consultants/engine/council.py`: `PLANNER_SYSTEM` (`:116`), `RESEARCHER_SYSTEM` (`:126`), `CRITIC_SYSTEM` (`:144`), `SYNTHESIZER_SYSTEM` (`:156`) to capture each role's job (decompose plan / execute research with tools / judge evidence sufficiency / write final user-facing answer).
3. Verify the module docstring's own description of the council as "a council of specialist agents (planner / researcher / critic / synthesizer)" at `consultants/__init__.py:3-5`.
4. Cross-check the pipeline ordering in the graph docstring `consultants/engine/graph.py:13-17` (planner → researcher → critic → synthesizer, with a needs_more loop back to researcher) and the per-role progress keys in `consultants/server/app.py:57-58`.
5. Note the role-specific behavior nuance: critic node is dropped at low/medium effort in `consultants/server/runner.py:58-59` and the synthesizer self-critics instead (`consultants/engine/council.py:177`) — confirm this does not change the *named* four roles.
6. Report back in one short sentence naming the four roles with their one-line functions, citing the `path:line` references above.

---

## Researcher (round 1)

Findings (all verified this turn):

- Canonical role tuple: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`
- Mandatory role: `MANDATORY_ROLES: frozenset[str] = frozenset({"synthesizer"})` — `consultants/config.py:41`; synthesizer is force-re-enabled on every config load — `consultants/config.py:219`; disabling it raises "role 'synthesizer' is mandatory and cannot be disabled" — `consultants/config.py:312-315`
- Pipeline order helper uses the same tuple order: `[r for r in ("planner", "researcher", "critic") if r in enabled_set]` then synthesizer — `consultants/engine/graph.py:202-220`
- Module docstring: "runs a council of specialist agents (planner / researcher / critic / synthesizer)" — `consultants/__init__.py:3-4`
- Preamble lists the same four roles as peer LLM agents — `consultants/engine/council.py:96-107` (specifically `:98-99`)
- Role job descriptions (system prompts):
  - **planner** — "Decompose the question into 3-7 concrete investigation steps the researcher will execute with project tools… You are NOT answering the question — your output is the researcher's plan." — `consultants/engine/council.py:116-124`
  - **researcher** — "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory). Cite findings as `path:line`… Do NOT answer the user's question — that's the synthesizer's job." — `consultants/engine/council.py:126-142`
  - **critic** — "Decide whether the researcher's evidence is sufficient for the synthesizer." Output `DECISION: ready` / `DECISION: needs_more_research` — `consultants/engine/council.py:144-154`
  - **synthesizer** — "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict." — `consultants/engine/council.py:156-168`
- Graph topology: `START -> [planner] -> [researcher] -> [critic] -> ?` with `ready -> [synthesizer] -> END` and `needs_more -> [researcher]` (loop) — `consultants/engine/graph.py:13-17`
- Effort-dependent variant: at effort < high the critic node is dropped and `SYNTHESIZER_SELF_CRITIC_SYSTEM` makes the synthesizer self-criticize — `consultants/engine/council.py:170-193`, `consultants/engine/graph.py:113-116`
- Config skill UI confirms the same four role names to users — `.claude/skills/consultants--config/SKILL.md:41-44`

No evidence gaps on the plan's three items.

---

## Researcher (round 1)

**Findings (lane 2, verified):**

- Canonical four roles are the tuple `ROLES = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; `synthesizer` is the only mandatory one (`consultants/config.py:41`, `consultants/config.py:219`).
- Package docstring names the same four: "specialist agents (planner / researcher / critic / synthesizer)" — `consultants/__init__.py:4`; also the council preamble lists them — `consultants/engine/council.py:98`.
- Graph topology: `START -> [planner] -> [researcher] -> [critic] -> ?`, critic `ready -> [synthesizer] -> END`, `needs_more -> [researcher]` (loop) — `consultants/engine/graph.py:13-17`; node builders for each role — `consultants/engine/graph.py:134-179`; pipeline order hardcoded as `("planner", "researcher", "critic")` + synthesizer — `consultants/engine/graph.py:202-220`; critic conditional edge (`route_after_critic`) — `consultants/engine/graph.py:412-425`.
- Per-role progress keys are exactly those four — `consultants/server/app.py:57-58` ("Keys: planner / researcher / critic / synthesizer").
- Role functions (system prompts): **planner** = decompose the question into 3–7 concrete investigation steps, output a numbered list only — `consultants/engine/council.py:116-123`; **researcher** = execute the plan with tools (`read_file`, `grep`, `glob`, `list_files`, `survey_project`, `recall_memory`), cite `path:line`, do not answer — `consultants/engine/council.py:126-141`; **critic** = judge whether the research evidence is sufficient, emit `DECISION: ready` or `DECISION: needs_more_research` — `consultants/engine/council.py:144-153`; **synthesizer** = write the final user-facing answer from plan + research + verdict — `consultants/engine/council.py:156-167`.
- Nuance check (plan item 2): at effort low/medium the critic node is removed from the enabled set — `consultants/server/runner.py:58-59` — but this does **not** change the named four; `ROLES` is unchanged (`consultants/config.py:40`) and the progress dict still carries a `critic` key (`consultants/server/app.py:57-58`).
- Correction to the plan's item 2: the synthesizer self-critic is *off by default at every tier* (`synthesizer_self_critic = False`, `consultants/server/runner.py:64-67`), so at low/medium the critique duty is dropped entirely rather than reassigned to the synthesizer; the self-critic prompt variant at `consultants/engine/council.py:177` is infrastructure only, currently unreferenced by the runner.

**Suggested one-sentence answer:** the council's four roles are the planner, which decomposes the question into a numbered investigation plan (`consultants/engine/council.py:116-123`), the researcher, which executes that plan with project tools and cites `path:line` evidence (`consultants/engine/council.py:126-141`), the critic, which rules the evidence `ready` or `needs_more_research` and can loop the researcher (`consultants/engine/council.py:144-153`, `consultants/engine/graph.py:13-17`), and the synthesizer, which writes the final answer from plan, research, and verdict (`consultants/engine/council.py:156-167`) — with the critic node optionally dropped at low/medium effort (`consultants/server/runner.py:58-59`).

---

## Synthesizer

The council's four roles are the **planner** (decomposes the question into 3–7 numbered investigation steps, `consultants/engine/council.py:116-123`), the **researcher** (executes that plan with project tools and cites `path:line` evidence, `consultants/engine/council.py:126-141`), the **critic** (rules the evidence `ready` or `needs_more_research`, looping the researcher when not, `consultants/engine/council.py:144-153`, `consultants/engine/graph.py:13-17`), and the **synthesizer** (writes the final user-facing answer from plan, research, and verdict, `consultants/engine/council.py:156-167`) — the canonical tuple `("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40`, matching the package docstring at `consultants/__init__.py:3-4`.

Two caveats on the runtime shape: the synthesizer is the only mandatory role (`consultants/config.py:41`), and at low/medium effort the critic node is dropped from the graph entirely rather than reassigned to the synthesizer, whose self-critic prompt is off by default at every tier (`consultants/server/runner.py:58-59`, `consultants/server/runner.py:64-67`).

---
