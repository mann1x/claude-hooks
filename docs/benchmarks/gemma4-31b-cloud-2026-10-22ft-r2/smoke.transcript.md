# Consultation transcript — csl-2026-10-22-2003-e363

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

1. Read `consultants/engine/council.py` to identify the system prompt constants for the council roles.
2. Check `consultants/engine/graph.py` to verify how these roles are wired into the StateGraph.
3. Verify if any additional roles are defined in `consultants/config.py`.

---

## Researcher (round 1)

The four roles of the consultants council are the planner (decomposes questions into investigation steps), the researcher (executes the plan using project tools), the critic (decides if evidence is sufficient), and the synthesizer (writes the final answer) (`consultants/engine/council.py:116-168`).

---

## Researcher (round 1)

The four roles of the consultants council are planner, researcher, critic, and synthesizer (`consultants/config.py:40`).

---

## Synthesizer

The four roles of the consultants council are the planner, researcher, critic, and synthesizer (`consultants/engine/council.py:116-168`, `consultants/config.py:40`).

---
