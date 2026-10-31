---
session_id: csl-2026-10-22-1719-8908
created: "2026-10-22T17:19:28"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 33.50
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rwwx
---

The council's four roles are the **planner** (decomposes the question into 3–7 numbered investigation steps, `consultants/engine/council.py:116-123`), the **researcher** (executes that plan with project tools and cites `path:line` evidence, `consultants/engine/council.py:126-141`), the **critic** (rules the evidence `ready` or `needs_more_research`, looping the researcher when not, `consultants/engine/council.py:144-153`, `consultants/engine/graph.py:13-17`), and the **synthesizer** (writes the final user-facing answer from plan, research, and verdict, `consultants/engine/council.py:156-167`) — the canonical tuple `("planner", "researcher", "critic", "synthesizer")` at `consultants/config.py:40`, matching the package docstring at `consultants/__init__.py:3-4`.

Two caveats on the runtime shape: the synthesizer is the only mandatory role (`consultants/config.py:41`), and at low/medium effort the critic node is dropped from the graph entirely rather than reassigned to the synthesizer, whose self-critic prompt is off by default at every tier (`consultants/server/runner.py:58-59`, `consultants/server/runner.py:64-67`).
