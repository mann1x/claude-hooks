# Consultation transcript — csl-2026-10-22-2112-46b2

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/config.py:40` to confirm the canonical role tuple `ROLES = ("planner", "researcher", "critic", "synthesizer")` and note `MANDATORY_ROLES` at `consultants/config.py:41`.
2. Grep `consultants/engine/council.py` for the per-role system prompts (`PLANNER_SYSTEM`, `RESEARCHER_SYSTEM`, `CRITIC_SYSTEM`, `SYNTHESIZER_SYSTEM` at `consultants/engine/council.py:116`, `:126`, `:144`, `:156`) to capture each role's one-line job description.
3. Confirm the council preamble's role enumeration at `consultants/engine/council.py:96-107` ("planner, researcher, critic, synthesizer") as an independent in-code statement of the four roles.
4. Verify the graph wiring orders the same four nodes (planner → researcher → critic → synthesizer, with critic routing back to researcher) in `consultants/engine/graph.py` so the final sentence reflects the actual pipeline order.
5. Check `consultants/engine/council.py:763` (`initial_state`) and the `RoleTurn(role=...)` literals at `:465`, `:646`, `:688`, `:750` to confirm the role names used in stored transcripts match the four canonical names.
6. Write the final single-sentence answer naming the four roles in pipeline order, with a `path:line` citation to `consultants/config.py:40`.

---

## Researcher (round 1)

Findings (all verified this turn):

- **Canonical role tuple**: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; `MANDATORY_ROLES: frozenset[str] = frozenset({"synthesizer"})` — `consultants/config.py:41`.
- **Independent in-code enumeration**: council preamble states "The other roles (planner, researcher, critic, synthesizer) are also LLMs…" — `consultants/engine/council.py:96-107` (enumeration at `consultants/engine/council.py:98-99`).
- **Per-role job descriptions** (each is a `_role_prompt(...)` wrapping the shared preamble — `consultants/engine/council.py:110-113`):
  - **planner** — "Decompose the question into 3-7 concrete investigation steps the researcher will execute with project tools… You are NOT answering the question — your output is the researcher's plan." Output: numbered list, nothing else. `consultants/engine/council.py:116-124`.
  - **researcher** — "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory). Cite findings as `path:line`." `consultants/engine/council.py:126-142`.
  - **critic** — "Decide whether the researcher's evidence is sufficient for the synthesizer." Emits `DECISION: ready` OR `DECISION: needs_more_research`. `consultants/engine/council.py:144-154`.
  - **synthesizer** — "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict." `consultants/engine/council.py:156-168`.
- **Variant**: `SYNTHESIZER_SELF_CRITIC_SYSTEM` — synthesizer absorbs critic duty when the critic node is dropped (low/medium effort); dedicated critic stays at high effort — `consultants/engine/council.py:170-180`.
- **Context**: `DEFAULT_TOPOLOGY = "council"`, `VALID_TOPOLOGIES = ("council",)` — `consultants/config.py:43-44`.

Caveat for synthesizer: the four roles are named in code as planner/researcher/critic/synthesizer; any gloss of *what* each does is a paraphrase of the prompt strings above.

---

## Researcher (round 1)

Findings (all cites verified this turn):

- **Canonical role tuple**: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; synthesizer is mandatory (`consultants/config.py:41`, enforced at `consultants/config.py:219`).
- **Pipeline order = same order**: module docstring topology `START -> [planner] -> [researcher] -> [critic] -> ?` with `ready -> [synthesizer] -> END` and `needs_more -> [researcher] (loop)` — `consultants/engine/graph.py:13-17`.
- **Edge builder hardcodes the same order**: `pipeline = [r for r in ("planner", "researcher", "critic") if r in enabled_set]` — `consultants/engine/graph.py:202-203`; last non-synth role → synthesizer, `synthesizer → END` — `consultants/engine/graph.py:217-220`.
- **Critic→researcher back-route confirmed as a conditional edge**: `sg.add_conditional_edges("critic", council.route_after_critic, {ROUTE_RESEARCHER: "researcher", ROUTE_SYNTHESIZER: "synthesizer"})` — `consultants/engine/graph.py:412-421`; router logic `route_after_critic` — `consultants/engine/council.py:366` (returns `"synthesizer"` on `ready` — `:371-372`; returns researcher route otherwise — `consultants/engine/council.py:389+` region, cap checks `:377-389`).
- **Planner line numbers in the plan are accurate**: `planner_node` — `consultants/engine/council.py:436`; `RoleTurn(role="planner", ...)` — `consultants/engine/council.py:465` (tombstone variant `:458`).
- **Researcher**: `researcher_node` — `consultants/engine/council.py:481`; `RoleTurn(role="researcher", ...)` — `consultants/engine/council.py:646-647` (tombstone `:569`).
- **Critic**: `critic_node` — `consultants/engine/council.py:667`; `RoleTurn(role="critic", ...)` — `consultants/engine/council.py:688-689` (success path `:702`).
- **Synthesizer**: `synthesizer_node` — `consultants/engine/council.py:715`; `RoleTurn(role="synthesizer", ...)` — `consultants/engine/council.py:750` (tombstone `:743`).
- **`initial_state` confirmed at the plan's line**: `def initial_state(...)` — `consultants/engine/council.py:763`; state keys include `research`, `critique`, `critic_decision`, `final_answer`, `turns` — `consultants/engine/council.py:772-780`.
- **Role names also echoed in the shared preamble** sent to every node: "The other roles (planner, researcher, critic, synthesizer)" — `consultants/engine/council.py:98-99`; per-role system prompts `PLANNER_SYSTEM` `:116`, `RESEARCHER_SYSTEM` `:126`, `CRITIC_SYSTEM` `:144`, `SYNTHESIZER_SYSTEM` `:156`.
- **Note (synthesizer self-critic variant)**: at low/medium effort the critic node can be dropped and the synthesizer takes critic duty internally (`SYNTHESIZER_SELF_CRITIC_SYSTEM` — `consultants/engine/council.py:177-182`; flag `synthesizer_self_critic` — `consultants/engine/graph.py:113-116`; comment about dropping the critic at low/medium — `consultants/engine/council.py:170-176`). This does not change the canonical four-role set.

Evidence sufficient for a single-sentence answer citing `consultants/config.py:40`; supporting pipeline-order cites: `consultants/engine/graph.py:13-17` and `consultants/engine/graph.py:202-203`.

---

## Synthesizer

The consultants council has four roles — the **planner**, **researcher**, **critic**, and **synthesizer** — declared canonically as `ROLES = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40` and wired in that pipeline order (planner → researcher → critic → synthesizer, with the critic looping back to the researcher when it demands more research) at `consultants/engine/graph.py:13-17`.

- planner: decomposes the question into an investigation plan (`consultants/engine/council.py:116`)
- researcher: executes the plan with project tools and cites `path:line` (`consultants/engine/council.py:126`)
- critic: judges evidence sufficiency, emitting `ready` or `needs_more_research` (`consultants/engine/council.py:144 [no needs_more_research at this line; module scope]`)
- synthesizer: writes the final user-facing answer (`consultants/engine/council.py:156`), and is the only mandatory role (`consultants/config.py:41`)

---
