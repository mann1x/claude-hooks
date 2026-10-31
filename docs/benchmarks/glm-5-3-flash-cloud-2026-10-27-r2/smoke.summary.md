---
session_id: csl-2026-10-27-1152-9045
created: "2026-10-27T11:52:58"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 16.72
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-GKFg
---

The council's four roles are **planner, researcher, critic, and synthesizer** (`consultants/config.py:40`), each with its own system prompt defining the duty: decomposing the question into steps, executing the plan with tools, judging evidence sufficiency, and writing the final answer (`consultants/engine/council.py:117,127,145,157`).
