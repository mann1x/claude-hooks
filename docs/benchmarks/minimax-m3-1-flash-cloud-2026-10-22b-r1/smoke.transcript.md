# Consultation transcript — csl-2026-10-22-1615-b76c

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Verify the canonical role tuple in `consultants/config.py:40` to identify all four council roles.
2. Confirm each role’s responsibilities in the system prompts at `consultants/engine/council.py:116`, `consultants/engine/council.py:126`, `consultants/engine/council.py:144`, and `consultants/engine/council.py:156`.
3. Cross-check the planner → researcher → critic → synthesizer pipeline in `consultants/engine/graph.py:11-20` before reporting the four names in one short sentence.

---

## Researcher (round 1)

**Findings (researcher lane 1)**

- Canonical role tuple: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`. `MANDATORY_ROLES = frozenset({"synthesizer"})` — `consultants/config.py:41`; synthesizer is force-enabled on every config load — `consultants/config.py:219`.
- Pipeline order is the tuple order: `enabled_roles()` returns roles "in canonical pipeline order" — `consultants/config.py:371-373`; graph edges built over `("planner", "researcher", "critic")` then synthesizer — `consultants/engine/graph.py:202`, `:220`.
- **Planner** — "Decompose the question into 3-7 concrete investigation steps the researcher will execute with project tools… your output is the researcher's plan. Output: numbered list, nothing else." — `consultants/engine/council.py:116-124`.
- **Researcher** — "Execute the planner's numbered plan with the available tools (read_file, grep, glob, list_files, survey_project, recall_memory)… Cite findings as `path:line`." — `consultants/engine/council.py:126-142`; it is the only role granted the six grounding tools — `consultants/engine/council.py:22-24`.
- **Critic** — "Decide whether the researcher's evidence is sufficient for the synthesizer… Output line 1: `DECISION: ready` OR `DECISION: needs_more_research`." — `consultants/engine/council.py:144-154`; routing on that verdict: ready→synthesizer, needs_more→researcher loop — `consultants/engine/graph.py:13-17`, `:415-419`.
- **Synthesizer** — "Write the final answer the user will see, consuming the planner's plan, researcher's report(s), and critic's verdict. Lead with the bottom line on the first line." — `consultants/engine/council.py:156-168`; at low/medium effort the critic node is dropped and the synthesizer self-critiques — `consultants/engine/council.py:170-193`.
- Pipeline rules: synthesizer mandatory; at least one of planner/researcher must be enabled — `consultants/config.py:376-391`.
- Per-role reasoning-effort defaults reflect the roles' character (researcher & synthesizer `high`, planner & critic `medium`) — `consultants/config.py:86-91`.
- CLI exposes the same four roles as `set-role` choices — `consultants/cli.py:435`; skill doc lists the same four with per-role models — `.claude/skills/consultants--config/SKILL.md:41-44`.

**Evidence gap:** none for the four roles and their responsibilities; the planner's cited line numbers (config.py:40, council.py:116/126/144/156) all verified accurate.

**One-sentence answer for synthesis:** The consultants council is a four-role LLM pipeline — a **planner** that decomposes the question into concrete investigation steps, a **researcher** that executes that plan with project tools and reports `path:line`-grounded findings, a **critic** that judges the evidence sufficient (`ready`) or requests more research, and a mandatory **synthesizer** that turns the plan, reports, and verdict into the final bottom-line answer.

---

## Researcher (round 1)

Findings (evidence, cited):

- The four council roles are declared as a fixed tuple: `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` — `consultants/config.py:40`; `synthesizer` is the only mandatory role — `consultants/config.py:41`, enforced at load — `consultants/config.py:219`.
- Pipeline topology: `START -> [planner] -> [researcher] -> [critic] -> ?` with critic's conditional edge to `synthesizer` (ready) or back to `researcher` (needs_more) — `consultants/engine/graph.py:13-17`; node wiring order fixed as `("planner", "researcher", "critic")` then synthesizer — `consultants/engine/graph.py:202-220`; critic conditional edge → researcher/synthesizer — `consultants/engine/graph.py:412-425`.
- Role functions (from system prompts in `consultants/engine/council.py`):
  - planner: decompose the question into 3-7 concrete investigation steps, outputs a numbered list, does not answer — `consultants/engine/council.py:116-123`.
  - researcher: execute the planner's numbered plan with tools, does not answer the question — `consultants/engine/council.py:126-141`.
  - critic: decide whether the researcher's evidence is sufficient; emits `DECISION: ready` or `DECISION: needs_more_research` — `consultants/engine/council.py:144-147`.
  - synthesizer: writes the final answer the user will see, consuming plan + research report + critic verdict, with `path:line` citations — `consultants/engine/council.py:156-160`; self-critic variant at effort low/medium (critic node dropped) — `consultants/engine/council.py:177-181`, `consultants/server/runner.py:48-67`.
- Per-role LLM think defaults keyed to the same four names — `consultants/config.py:86-91`; per-role progress keys `planner / researcher / critic / synthesizer` — `consultants/server/app.py:57-59`; role turn records `role` is planner/researcher/critic/synthesizer — `consultants/engine/storage.py:41-43`.

Confidence: high for names and topology; role semantics grounded in the prompt strings cited above.

---

## Synthesizer

The consultants council is a four-role LLM pipeline (`consultants/config.py:40`) — a **planner** that decomposes the question into 3-7 concrete investigation steps (`consultants/engine/council.py:116`), a **researcher** that executes that plan with project tools and reports `path:line`-grounded findings (`consultants/engine/council.py:126`), a **critic** that rules the evidence `ready` or `needs_more_research` (`consultants/engine/council.py:144 [no needs_more_research at this line; module scope]`), and a **synthesizer** that turns plan, reports, and verdict into the final bottom-line answer (`consultants/engine/council.py:156`).

---
