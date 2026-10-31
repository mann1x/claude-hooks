---
session_id: csl-2026-10-22-0813-0e7f
created: "2026-10-22T08:13:11"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 39.07
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-7fei
---

The consultants council has four roles — **planner** (decomposes the question into an investigation plan), **researcher** (executes the plan with project tools and reports `path:line` findings), **critic** (judges whether the evidence suffices, emitting `DECISION: ready` or `needs_more_research`), and **synthesizer** (writes the final user-facing answer) — defined as `ROLES = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40` with per-role duties in `consultants/engine/council.py:116`, `:126`, `:144`, `:156` and pipeline order `START → planner → researcher → critic → synthesizer` at `consultants/engine/graph.py:13-15`; only the synthesizer is mandatory (`consultants/config.py:41`).
