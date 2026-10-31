# Consultation transcript — csl-2026-10-22-0813-0e7f

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/config.py:40` to confirm the canonical role tuple `ROLES = ("planner", "researcher", "critic", "synthesizer")` and note `MANDATORY_ROLES` (`consultants/config.py:41`).
2. Read the four role system prompts in `consultants/engine/council.py` — `PLANNER_SYSTEM` (`:116`), `RESEARCHER_SYSTEM` (`:126`), `CRITIC_SYSTEM` (`:144`), `SYNTHESIZER_SYSTEM` (`:156`) — to extract each role's one-line job description for the final sentence.
3. Cross-check the pipeline order/topology in `consultants/engine/graph.py:11-19` (START → planner → researcher → critic → synthesizer) so the sentence names roles in canonical order.
4. Verify the role names appear in the council preamble at `consultants/engine/council.py:96-99` ("The other roles (planner, researcher, critic, synthesizer)…") as a second independent source.
5. Confirm the same four names surface in the CLI/HTTP config payload (`consultants/cli.py:209-211`, `consultants/server/app.py:378-382`) so the answer is not limited to prompt text.

---

## Researcher (round 1)

Findings (all verified by direct read this turn):

- **Canonical role set / order:** `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`. `MANDATORY_ROLES = frozenset({"synthesizer"})` — `consultants/config.py:41`; synthesizer can never be disabled (`consultants/config.py:219`, `consultants/config.py:312`).
- **Pipeline order helper:** `enabled_roles()` returns roles "in canonical pipeline order" — `consultants/config.py:371-373`; `plan_topology()` builds edges from the fixed order `("planner", "researcher", "critic")` → `synthesizer` — `consultants/engine/graph.py:202-203`, `:212-220`.
- **Topology (graph docstring):** `START -> [planner] -> [researcher] -> [critic] -> ?` with `ready -> [synthesizer] -> END` and `needs_more -> [researcher]` (loop) — `consultants/engine/graph.py:13-17`. Roles disabled in config are omitted; synthesizer always present — `consultants/engine/graph.py:19-20`.
- **Planner role:** "Decompose the question into 3-7 concrete investigation steps the researcher will execute with project tools… You are NOT answering the question — your output is the researcher's plan." — `consultants/engine/council.py:116-124` (`PLANNER_SYSTEM`).
- **Researcher role:** "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory). Cite findings as `path:line`… Do NOT answer the user's question — that's the synthesizer's job." — `consultants/engine/council.py:126-142` (`RESEARCHER_SYSTEM`).
- **Critic role:** "Decide whether the researcher's evidence is sufficient for the synthesizer," emitting `DECISION: ready` or `DECISION: needs_more_research` — `consultants/engine/council.py:144-154` (`CRITIC_SYSTEM`); routing constants `ROUTE_SYNTHESIZER` / `ROUTE_RESEARCHER` — `consultants/engine/council.py:362-363`, `route_after_critic` — `consultants/engine/council.py:366-390`.
- **Synthesizer role:** "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict. Lead with the bottom line on the first line." — `consultants/engine/council.py:156-168` (`SYNTHESIZER_SYSTEM`). Self-critic variant (critic folded in at effort<high) — `consultants/engine/council.py:177-193`.
- **Preamble** naming the same four roles as LLM peers — `consultants/engine/council.py:96-107`.

Caveats: `docs/` contains no consultants role description (grep for "planner" in `docs/` only hit a Postgres runbook line, `docs/pgvector-runbook.md:83`), so the council role semantics are defined solely in `consultants/`. Effort tier affects whether the critic node exists (`EFFORT_CAPS` — `consultants/engine/council.py:59-75`; graph self-critic switch — `consultants/engine/graph.py:113-116`).

---

## Researcher (round 1)

**Findings (all verified this turn):**

- Authoritative role list: `consultants/config.py:40` — `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")`; `consultants/config.py:41` — `MANDATORY_ROLES: frozenset[str] = frozenset({"synthesizer"})` (synthesizer can never be disabled; `consultants/config.py:219`).
- Independent second source (prompt preamble): `consultants/engine/council.py:98-99` — "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop." `consultants/engine/council.py:89` — "without scattering tone directives across four prompts."
- Per-role duties from the role system prompts: `consultants/engine/council.py:117` planner decomposes the question into investigation steps; `consultants/engine/council.py:127` researcher executes the plan with tools; `consultants/engine/council.py:145` critic decides `ready` vs `needs_more_research`; `consultants/engine/council.py:157` synthesizer writes the final user-facing answer.
- Config/HTTP surface confirms the same four names, not just prompt text: `consultants/cli.py:202-211` builds a `roles` dict by iterating `cc.ROLES` and reports `mandatory_roles`; `consultants/server/app.py:373-382` does the same in `_config_snapshot`.
- Graph topology: `consultants/engine/graph.py:13-17` — `START -> [planner] -> [researcher] -> [critic] -> (ready) [synthesizer] -> END`, with a `needs_more` loop back to researcher.
- Also documented in the package docstring: `consultants/__init__.py:4` — "a council of specialist agents (planner / researcher / critic / synthesizer)".

**Answer content for synthesizer:** the four roles are planner, researcher, critic, and synthesizer (mandatory: synthesizer only).

No further tool calls needed; evidence is complete and cross-verified.

---

## Synthesizer

The consultants council has four roles — **planner** (decomposes the question into an investigation plan), **researcher** (executes the plan with project tools and reports `path:line` findings), **critic** (judges whether the evidence suffices, emitting `DECISION: ready` or `needs_more_research`), and **synthesizer** (writes the final user-facing answer) — defined as `ROLES = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40` with per-role duties in `consultants/engine/council.py:116`, `:126`, `:144`, `:156` and pipeline order `START → planner → researcher → critic → synthesizer` at `consultants/engine/graph.py:13-15`; only the synthesizer is mandatory (`consultants/config.py:41`).

---
