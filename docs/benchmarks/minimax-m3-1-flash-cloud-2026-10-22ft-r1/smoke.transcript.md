# Consultation transcript — csl-2026-10-22-2028-4ca7

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/config.py:40` and confirm the canonical role tuple `ROLES = ("planner", "researcher", "critic", "synthesizer")` and `MANDATORY_ROLES = frozenset({"synthesizer"})` (config.py:41).
2. Read `consultants/engine/council.py:96-99` to confirm the `COUNCIL_PREAMBLE` string enumerates the same four roles by name.
3. Read `consultants/engine/council.py:116-168` (`PLANNER_SYSTEM`, `RESEARCHER_SYSTEM`, `CRITIC_SYSTEM`, `SYNTHESIZER_SYSTEM`) and capture each role's one-line function: planner decomposes into a numbered plan; researcher executes the plan with project tools and cites `path:line`; critic issues `DECISION: ready` / `needs_more_research`; synthesizer writes the user-facing final answer.
4. Cross-check `consultants/engine/graph.py:13-17` (topology `START -> planner -> researcher -> critic -> synthesizer`, with a `needs_more` loop back to researcher) and `consultants/__init__.py:3-4` to confirm the four-role council framing.
5. Verify no fifth role exists by grepping for other role names in `consultants/` (e.g. `roles\[`, `enabled_roles`, and any `MANDATORY_ROLES`/node list) so the count of four is airtight.

---

## Researcher (round 1)

Findings (all verified by tool read this turn):

- Canonical role tuple: `ROLES = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; `MANDATORY_ROLES = frozenset({"synthesizer"})` — `consultants/config.py:41` (synthesizer is forced enabled on every config merge, `consultants/config.py:219`).
- Preamble enumerates the same four roles by name: "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop" — `consultants/engine/council.py:96-107` (string starts `:96`, role list at `:98-99`).
- Role jobs (from `*_SYSTEM` prompts, all built via `_role_prompt`, `consultants/engine/council.py:110-113`):
  - planner — decomposes the question into 3-7 concrete investigation steps for the researcher; output = numbered list only, does not answer — `consultants/engine/council.py:116-124`.
  - researcher — executes the planner's numbered plan with `read_file`/`grep`/`glob`/`list_files`/`survey_project`/`recall_memory`, citing `path:line`; explicitly not the answerer — `consultants/engine/council.py:126-142`.
  - critic — decides evidence sufficiency; line 1 is `DECISION: ready` or `DECISION: needs_more_research` — `consultants/engine/council.py:144-154`.
  - synthesizer — writes the final user-facing answer from plan + research + verdict, leading with the bottom line, citing `path:line` — `consultants/engine/council.py:156-168`.
- Node implementations match: `planner_node` `consultants/engine/council.py:436`; `researcher_node` `:481`; `critic_node` `:667` (decision parsed at `:696`, re-route delta at `:699`, failure defaults to `"ready"` at `:686`); `synthesizer_node` `:715` (returns `final_answer` at `:754`).
- Graph wiring uses those four node names: `consultants/engine/graph.py:332` (researcher), `:335` (critic), `:425` (critic → synthesizer edge), with planner/researcher/critic/synthesizer wrappers at `:136`, `:147`, `:162`, `:173`.
- Config validity rule: synthesizer must be enabled and at least one of planner/researcher must be enabled — `consultants/config.py:376-391`; per-role reasoning-effort defaults at `consultants/config.py:86-91`.
- Note: at effort `low`/`medium` the critic node is dropped and the synthesizer self-critiques via `SYNTHESIZER_SELF_CRITIC_SYSTEM` — `consultants/engine/council.py:177-193`, caps at `consultants/engine/council.py:59-75`.

---

## Researcher (round 1)

Findings (all verified this turn):

- **Four roles, canonical tuple:** `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; `MANDATORY_ROLES = frozenset({"synthesizer"})` at `consultants/config.py:41`.
- **Package docstring framing:** "a council of specialist agents (planner / researcher / critic / synthesizer)" — `consultants/__init__.py:3-4`.
- **Topology (plan's lines confirmed):** `START -> [planner] -> [researcher] -> [critic] -> ?` with `+-- ready ------> [synthesizer] -> END` and `+-- needs_more --> [researcher] (loop)` — `consultants/engine/graph.py:13-17`.
- **Pipeline order is hard-coded to those three + synthesizer:** `pipeline = [r for r in ("planner", "researcher", "critic") if r in enabled_set]` — `consultants/engine/graph.py:202-203`; nodes added per role at `consultants/engine/graph.py:331-340`.
- **No fifth role:** exhaustive grep over `consultants/` for `advisor|judge|oracle|verifier|ROLES|enabled_roles` returns only the four names plus `ROUTE_SYNTHESIZER`/`ROUTE_RESEARCHER` constants (`consultants/engine/council.py:362-363`) and the self-critic *variant* of the synthesizer prompt, which is not a separate role (`consultants/engine/council.py:177-181`).
- **Per-role node functions exist for exactly four:** `planner_node` `consultants/engine/council.py:436`, `researcher_node` `:481`, `critic_node` `:667`, `synthesizer_node` `:715`; routing `route_after_critic` at `:366`.
- **Role duties (from system prompts):** planner decomposes into 3-7 investigation steps (`:116-123`); researcher executes the plan with project tools (`:126-141`); critic decides `DECISION: ready` vs `needs_more_research` (`:144-148`); synthesizer writes the final user-facing answer citing `path:line` (`:156-160`).
- **CLI/skill surface agrees:** CLI exposes role set via `cc.ROLES` (`consultants/cli.py:209`, `:435`) and `mandatory_roles` (`consultants/cli.py:211`); skill lists the same four (`.claude/skills/consultants/SKILL.md:3`, `:9`; `.claude/skills/consultants--config/SKILL.md:41-44`).
- **Nuance worth flagging to synthesizer:** at `effort` low/medium the critic node is *dropped* from the graph and the synthesizer absorbs critic duty via `SYNTHESIZER_SELF_CRITIC_SYSTEM` (`consultants/server/runner.py:48-67`; prompt at `consultants/engine/council.py:177-181`) — so the four-role count is the canonical roster, not always the runtime path.

---

## Synthesizer

The four roles are the planner (decomposes the question), researcher (investigates it), critic (assesses evidence sufficiency), and synthesizer (writes the final answer) (`consultants/config.py:40`; `consultants/engine/council.py:116-168`).

---
