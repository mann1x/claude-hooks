# Consultation transcript — csl-2026-10-22-1851-ce44

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/engine/council.py` to identify the four roles and their specific job descriptions within the `_SYSTEM` prompt constants.
2. Extract the core responsibility for each role (planner, researcher, critic, and synthesizer) to formulate the final answer.

---

## Researcher (round 1)

The consultants council consists of the **planner**, who decomposes questions into investigation steps (`consultants/engine/council.py:117`); the **researcher**, who executes the plan using project tools to provide evidence (`consultants/engine/council.py:127`); the **critic**, who determines if the evidence is sufficient (`consultants/engine/council.py:145`); and the **synthesizer**, who writes the final answer based on the preceding outputs (`consultants/engine/council.py:157`).

---

## Researcher (round 1)

The roles of the consultants council are: the **planner** decomposes the question into investigation steps (`consultants/engine/council.py:117`), the **researcher** executes that plan using project tools (`consultants/engine/council.py:127`), the **critic** decides if the evidence is sufficient for the synthesizer (`consultants/engine/council.py:145`), and the **synthesizer** writes the final answer for the user (`consultants/engine/council.py:157`).

---

## Synthesizer

The council consists of the planner, who decomposes questions into investigation steps (`consultants/engine/council.py:117`), the researcher, who executes the plan using tools to provide evidence (`consultants/engine/council.py:127`), the critic, who determines if the evidence is sufficient (`consultants/engine/council.py:145`), and the synthesizer, who writes the final answer (`consultants/engine/council.py:157`).

---
