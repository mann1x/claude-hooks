---
session_id: csl-2026-10-22-1700-0ccd
created: "2026-10-22T17:00:54"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 29.16
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-YKSM
---

The consultants council has four roles — **planner** (decomposes the question into 3–7 tool-directed investigation steps, `consultants/engine/council.py:116-124`), **researcher** (executes that plan with the project tools and cites findings as `path:line`, `consultants/engine/council.py:126-142`), **critic** (rules whether the evidence is sufficient, emitting `DECISION: ready` or `DECISION: needs_more_research`, `consultants/engine/council.py:144-154`), and **synthesizer** (writes the final user-facing answer, `consultants/engine/council.py:156-168`).

The set is canonical in `ROLES: tuple[str, ...] = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40`, with only `synthesizer` mandatory (`consultants/config.py:41`), and the pipeline order is planner → researcher → critic → synthesizer, looping back to the researcher on `needs_more` (`consultants/engine/graph.py:13-17`).
