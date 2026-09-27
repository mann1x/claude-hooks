# Consultation transcript — csl-2026-09-23-1810-b954

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/engine/council.py:96-100` and `:117-165` to extract the exact four role names from the shared system prompts (`ROLE: planner`, `researcher`, `critic`, `synthesizer`).
2. Confirm the same four-role list appears in the role registry/graph wiring — grep `consultants/engine/graph.py` and `consultants/config.py` for `planner|researcher|critic|synthesizer` to verify no fifth default role.
3. Check whether `tool_executor` and `coder` are default-off opt-ins (grep `consultants/config.py` and `consultants/engine/graph.py` for `tool_executor|coder|enabled`) so the answer can note that the "four roles" claim excludes them.
4. Verify each role's one-line function from its prompt text: `council.py:117-122` (planner decomposes), `:127-140` (researcher executes plan with tools), `:145-155` (critic judges sufficiency), `:157-168` (synthesizer writes final answer).
5. Corroborate user-facing wording in the dispatcher skill `.claude/skills/consultants--list/SKILL.md` (and `consultants/cli.py` help text) for a canonical phrasing to quote in the single sentence.

---

## Researcher (round 1)

## Findings

- **Four roles, verbatim from code**: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`. Adjacent `MANDATORY_ROLES = frozenset({"synthesizer"})` at `consultants/config.py:41` (only synth is non-optional).
- **Shared preamble names the same four**: `COUNCIL_PREAMBLE` — "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop" — `consultants/engine/council.py:96-99`.
- **Per-role system prompts confirm the four job descriptions** (planner=decompose question into 3-7 steps; researcher=execute plan with tools + cite `path:line`; critic=decide evidence sufficiency → `DECISION: ready|needs_more_research`; synthesizer=write final user-facing answer): `consultants/engine/council.py:117` (planner), `:127` (researcher), `:145` (critic), `:157` (synthesizer). Self-critic variant at `consultants/engine/council.py:178`.
- **Package docstring states it canonically**: "runs a council of specialist agents (planner / researcher / critic / synthesizer)" — `consultants/__init__.py:3-4`.
- **Graph topology = same four nodes**, `START -> [planner] -> [researcher] -> [critic] -> ? -> [synthesizer] -> END` — `consultants/engine/graph.py:13-17`; pipeline tuple `("planner", "researcher", "critic")` at `consultants/engine/graph.py:202`; the four node wrappers at `consultants/engine/graph.py:134/145/160/171`.
- **No fifth default role**: exhaustive grep for `coder|orchestrator|executor` under `consultants/` returns only `tool_executor` (a callable dependency, not a role) — `consultants/engine/graph.py:109`, `consultants/engine/council.py:483`, `consultants/server/runner.py:106`, plus `ThreadPoolExecutor` in `consultants/server/app.py:116`. No `coder` role exists.
- **But the four are not always all fired**: at `effort=low|medium` the critic node is dropped from `enabled` → bare `planner -> researcher -> synthesizer` — `consultants/server/runner.py:58-59`; self-critic synthesizer hardcoded off at `consultants/server/runner.py:67`.
- **Per-role think defaults** (planner=medium, researcher=high, critic=medium, synthesizer=high) — `consultants/config.py:86-91`.
- **Bench ground truth for exactly this question** — expected answer tokens are the four names: `docs/consultants-benchmarks.md:35-36` and `docs/benchmarks/EVALUATION.md:68-70` ("Ground truth: `planner`, `researcher`, `critic`, `synthesizer`"); the smoke query itself is verbatim at `docs/consultants-benchmarks.md:41`.
- **Fabrication warning already encoded in the bench rubric**: naming "synthesizer / critic / orchestrator / executor" is a FAIL — `docs/benchmarks/EVALUATION.md:76`.

**Answer sentence (for the synthesizer):** The consultants council has four roles — the **planner** (decomposes the question into investigation steps), **researcher** (executes the plan with project tools and reports `path:line` evidence), **critic** (decides whether that evidence is sufficient), and **synthesizer** (writes the final user-facing answer) — grounded at `consultants/config.py:40`, `consultants/engine/council.py:117-168`, `consultants/__init__.py:3-4`.

---

## Researcher (round 1)

Evidence for the four council roles, all verified this turn:

- **Four roles enumerated in one place** — `consultants/config.py:40` defines `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")`; `consultants/config.py:41` marks `synthesizer` the only mandatory one.
- **Preamble names the same four** — `consultants/engine/council.py:98-99`: "The other roles (planner, researcher, critic, synthesizer) are also LLMs reading your output as input on the next hop."
- **planner** — `consultants/engine/council.py:117-123`: "Decompose the question into 3-7 concrete investigation steps the researcher will execute with project tools … your output is the researcher's plan."
- **researcher** — `consultants/engine/council.py:127-142`: "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory). Cite findings as `path:line`."
- **critic** — `consultants/engine/council.py:145-154`: "Decide whether the researcher's evidence is sufficient for the synthesizer," emitting `DECISION: ready` or `DECISION: needs_more_research`.
- **synthesizer** — `consultants/engine/council.py:157-168`: "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict."
- **Corroborating framing** — `consultants/__init__.py:4`: "a council of specialist agents (planner / researcher / critic / synthesizer)"; topology diagram at `consultants/engine/graph.py:13-17`: `START -> [planner] -> [researcher] -> [critic] -> ?` with `ready → [synthesizer] -> END` and `needs_more → [researcher] (loop)`.

Cite-accuracy note: planner's prompt block is `council.py:116-124` (plan said 117-122, actual body 117-123). Skill `/.claude/skills/consultants--list/SKILL.md:1-38` is a *session lister* and contains no role definitions — it cannot corroborate role wording; the only user-facing enumeration is the ROLES tuple and the prompts above.

Canonical one-sentence phrasing supported by the cites: the **planner** decomposes the question into concrete investigation steps, the **researcher** executes that plan with project tools and returns `path:line`-cited findings, the **critic** decides whether that evidence is sufficient (else loops back for more), and the **synthesizer** writes the final user-facing answer from plan + research + verdict.

---

## Synthesizer

The consultants council has four roles — the **planner** (decomposes the question into concrete investigation steps), the **researcher** (executes that plan with project tools and reports `path:line`-cited evidence), the **critic** (judges whether the evidence is sufficient, looping back if not), and the **synthesizer** (writes the final user-facing answer) — defined in `consultants/config.py:40` and named in `consultants/engine/council.py:98-99`, with each prompt at `council.py:117-123` (planner), `:127-142` (researcher), `:145-154` (critic), `:157-168` (synthesizer). No fifth role exists: `tool_executor` is a callable dependency, not a role (`consultants/engine/graph.py:109`).

---
