# Consultation transcript — csl-2026-09-23-1803-3667

_Question_: From the code in this repository, name the four roles of the consultants council in a single short sentence.


## Planner

The four roles of the consultants council are **planner, researcher, critic, and synthesizer** (`consultants/config.py:40`, `ROLES` tuple; also named in the council preamble at `consultants/engine/council.py:96-99`).

---

## Researcher

**Report:**

- The four council roles are defined as a pipeline-ordered tuple: `ROLES = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40`.
- The council preamble names the same four roles in text at `consultants/engine/council.py:98-99` ("The other roles (planner, researcher, critic, " / "synthesizer) are also LLMs reading your output…").
- Per-role prompts confirm each role exists as a node: `planner` (`consultants/engine/council.py:117`, `planner_node` at `:436`), `researcher` (`:127`), `critic` routing (`:379`), `synthesizer` (`:157`, `synthesizer_node` at `:715`).
- The synthesizer is mandatory (`MANDATORY_ROLES = frozenset({"synthesizer"})`, `consultants/config.py:41`), and pipeline validity requires at least one of planner/researcher (`consultants/config.py:388`).

**Answer:** The consultants council consists of four roles — **planner, researcher, critic, and synthesizer** (`consultants/config.py:40`), with only the synthesizer mandatory (`consultants/config.py:41`).

---

## Synthesizer

The four roles of the consultants council are **planner, researcher, critic, and synthesizer** (`consultants/config.py:40`).

---
