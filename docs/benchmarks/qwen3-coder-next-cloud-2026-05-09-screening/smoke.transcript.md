# Consultation transcript — csl-2026-05-09-0650-4b7d

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Examine `src/roles.rs` to identify role definitions and their struct definitions.  
2. Search `src/council.rs` for how the council is instantiated and how roles are assigned.  
3. Look for `ROLE` or `COUNCIL` constants in configuration or constants files (e.g., `config/mod.rs`, `src/constants.rs`).  
4. Review `README.md` or `docs/roles.md` for explicit role descriptions or documentation.  
5. Inspect test files (e.g., `tests/council_tests.rs`) that reference role names or usage.  
6. Search for role-related enums in `src/` using `grep -r "enum.*Role"` or similar.  
7. Check Cargo.toml for any role-specific feature flags that might indicate responsibilities.

---

## Researcher (round 1)

The four roles of the consultants council are: planner (decomposes questions into concrete investigation steps), researcher (executes the plan with project tools and reports findings), critic (assesses sufficiency of research evidence), and synthesizer (writes the final user-facing answer).

```json
{
  "roles": "The consultants council has four roles: planner (decomposes questions into concrete investigation steps), researcher (executes the plan with project tools and reports findings), critic (assesses sufficiency of research evidence), and synthesizer (writes the final user-facing answer).",
  "citations": [
    "consultants/engine/council.py:117",
    "consultants/engine/council.py:127",
    "consultants/engine/council.py:145",
    "consultants/engine/council.py:157"
  ]
}
```

---

## Researcher (round 1)

Based on the tool outputs above, here is the focused report:

- The consultants council consists of exactly four roles: **planner**, **researcher**, **critic**, and **synthesizer**, declared as `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` in `consultants/config.py:40`.  
- Each role has a dedicated node function in `consultants/engine/council.py`: `planner_node`, `researcher_node`, `critic_node`, and `synthesizer_node` at lines 436, 481, 667, and 715 respectively.  
- The roles are wired in a sequential pipeline topology in `consultants/engine/graph.py:13-17` with conditional routing from critic: `START -> planner -> researcher -> critic -> [ready -> synthesizer]` or `[needs_more -> researcher (loop)]`.  
- Role-specific system prompts are defined in `consultants/engine/council.py:116-193` (`PLANNER_SYSTEM`, `RESEARCHER_SYSTEM`, `CRITIC_SYSTEM`, `SYNTHESIZER_SYSTEM`).  
- All roles share a common `COUNCIL_PREAMBLE` (`consultants/engine/council.py:96-107`) that enforces concise, LLM-to-LLM communication without filler or sign-offs.

---

## Researcher (round 1)

The four roles of the consultants council are planner, researcher, critic, and synthesizer.

- Planner: decomposes the question into concrete investigation steps for the researcher.
- Researcher: executes the plan using project tools (read_file, grep, glob, list_files, survey_project, recall_memory) and cites findings as `path:line`.
- Critic: decides whether the researcher’s evidence is sufficient, outputting `DECISION: ready` or `DECISION: needs_more_research`.
- Synthesizer: writes the final answer, consuming all prior outputs and citing `path:line` for every codebase-dependent claim (`consultants/engine/council.py:116-168`).

```bash
grep -r "ROLE: planner\|ROLE: researcher\|ROLE: critic\|ROLE: synthesizer" consultants/engine/council.py | head -4
```

```python
# Roles defined as constants in consultants/engine/council.py:
PLANNER_SYSTEM = _role_prompt(...)  # line 116
RESEARCHER_SYSTEM = _role_prompt(...)  # line 126
CRITIC_SYSTEM = _role_prompt(...)  # line 144
SYNTHESIZER_SYSTEM = _role_prompt(...)  # line 156
```

```bash
grep -n "^def build_.*_messages" consultants/engine/council.py
# 196: build_planner_messages
# 203: build_researcher_messages
# 231: build_critic_messages
# 245: build_synthesizer_messages
```

---

## Synthesizer

The four roles of the consultants council are planner (decomposes questions into concrete investigation steps at `consultants/engine/council.py:116`), researcher (executes plans using project tools and cites `path:line` at `consultants/engine/council.py:126`), critic (assesses evidence sufficiency and decides `ready` or `needs_more_research` at `consultants/engine/council.py:144`), and synthesizer (writes the final user-facing answer, citing `path:line` for codebase claims at `consultants/engine/council.py:156`).

---
