---
session_id: csl-2026-10-22-2049-1cba
created: "2026-10-22T20:49:46"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 31.22
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-i6US
---

The consultants council has four roles: planner, researcher, critic, and synthesizer (`consultants/config.py:40`), run in that pipeline order (`consultants/engine/graph.py:13-17`).
