---
session_id: csl-2026-10-22-0758-e303
created: "2026-10-22T07:58:01"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 51.21
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-YDhi
---

The consultants council has four roles: **planner** (decomposes the question into concrete investigation steps — `consultants/engine/council.py:117`), **researcher** (executes the plan with project tools and cites findings as `path:line` — `consultants/engine/council.py:127`), **critic** (judges whether the evidence is sufficient, emitting `DECISION: ready` / `needs_more_research` — `consultants/engine/council.py:145 [no needs_more_research at this line; module scope]`), and **synthesizer** (writes the final user-facing answer from the plan, reports, and verdict — `consultants/engine/council.py:157`).

This is the canonical set: `ROLES = ("planner", "researcher", "critic", "synthesizer")` (`consultants/config.py:40`), matching the preamble's role list (`consultants/engine/council.py:98`) and one node per role (`consultants/engine/graph.py:325`). Note the critic is only executed at effort high/max — at low/medium it is dropped and the synthesizer self-critic path is currently disabled (`consultants/server/runner.py:58`, `consultants/server/runner.py:67`).
