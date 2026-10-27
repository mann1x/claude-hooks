---
session_id: csl-2026-10-27-1220-58b1
created: "2026-10-27T12:20:09"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 21.56
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-WSBV
---

The consultants council's four roles are the **planner** (decomposes the question into investigation steps), the **researcher** (executes those steps with tools and reports findings), the **critic** (verdicts and decides if more research is needed), and the **synthesizer** (writes the final answer) — enumerated in the council preamble at `consultants/engine/council.py:98-99` and as the canonical `ROLES` tuple at `consultants/config.py:40`.
