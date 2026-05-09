# Consultation transcript — csl-2026-05-09-0614-368c

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Search for "council" or "consultants council" across the repository to identify the defining module or documentation.
2. Inspect `README.md` for the functional description of the council's structure and roles.
3. Locate the agent initialization logic in `src/` or a similar core directory to identify the four specific class names or identifiers.
4. Examine the prompt templates or configuration files (e.g., `.yaml`, `.json`, or `prompts.py`) to confirm the specific duties assigned to each role.
5. Verify the role names against the system prompt or orchestration logic used to coordinate the LLM-to-LLM handoffs.

---

## Researcher (round 1)

The consultants council consists of four roles: **planner**, **researcher**, **critic**, and **synthesizer**.

*   **planner**: Decomposes the user question into concrete investigation steps for the researcher (`consultants/engine/council.py:116-124`).
*   **researcher**: Executes the plan using project tools to find evidence and writes focused reports (`consultants/engine/council.py:126-141`).
*   **critic**: Decides if the researcher's evidence is sufficient or if more research is needed (`consultants/engine/council.py:144-153`).
*   **synthesizer**: Writes the final answer for the user based on the plan, research reports, and critic's verdict (`consultants/engine/council.py:156-168`).

These roles are explicitly named in the `COUNCIL_PREAMBLE` at `consultants/engine/council.py:98-99`.

---

## Researcher (round 1)

The consultants council is composed of four specialized roles: a **planner** who decomposes the user's question into concrete investigation steps (`consultants/engine/council.py:116-124`), a **researcher** who executes those steps using project tools to find evidence and report findings (`consultants/engine/council.py:126-142`), a **critic** who evaluates whether the gathered evidence is sufficient or requires further research (`consultants/engine/council.py:144-154`), and a **synthesizer** who compiles the plan, research, and critique into a final answer for the user (`consultants/engine/council.py:156-168`).

- **Planner**: Decomposes questions into 3-7 investigation steps for the researcher (`consultants/engine/council.py:117`).
- **Researcher**: Executes the plan using tools like `grep` and `read_file`, citing findings as `path:line` (`consultants/engine/council.py:127-129`).
- **Critic**: Determines if research is "ready" or "needs_more_research" based on evidence sufficiency (`consultants/engine/council.py:145-147`).
- **Synthesizer**: Writes the final codebase-dependent response while hiding the council's internal process from the user (`consultants/engine/council.py:157-165`).

---

## Researcher (round 1)

The four roles of the consultants council are the **planner**, **researcher**, **critic**, and **synthesizer**, as defined in `consultants/config.py:40` and detailed in `consultants/engine/council.py:116-168`.

*   **planner**: Decomposes the user question into concrete investigation steps for the researcher (`consultants/engine/council.py:117-118`).
*   **researcher**: Executes the planner's investigation steps using project tools and reports findings (`consultants/engine/council.py:127-129`).
*   **critic**: Evaluates whether the researcher's evidence is sufficient or if additional research rounds are required (`consultants/engine/council.py:145-146`).
*   **synthesizer**: Consumes the plan, research reports, and critic's verdict to write the final answer for the user (`consultants/engine/council.py:157-159`).

---

## Synthesizer

The consultants council comprises the planner, researcher, critic, and synthesizer (`consultants/config.py:40`; `consultants/engine/council.py:98-99`).

---
