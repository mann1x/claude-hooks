---
session_id: csl-2026-10-22-2112-46b2
created: "2026-10-22T21:12:56"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 28.63
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rb1F
---

The consultants council has four roles — the **planner**, **researcher**, **critic**, and **synthesizer** — declared canonically as `ROLES = ("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40` and wired in that pipeline order (planner → researcher → critic → synthesizer, with the critic looping back to the researcher when it demands more research) at `consultants/engine/graph.py:13-17`.

- planner: decomposes the question into an investigation plan (`consultants/engine/council.py:116`)
- researcher: executes the plan with project tools and cites `path:line` (`consultants/engine/council.py:126`)
- critic: judges evidence sufficiency, emitting `ready` or `needs_more_research` (`consultants/engine/council.py:144 [no needs_more_research at this line; module scope]`)
- synthesizer: writes the final user-facing answer (`consultants/engine/council.py:156`), and is the only mandatory role (`consultants/config.py:41`)
