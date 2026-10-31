---
session_id: csl-2026-10-27-1126-f09d
created: "2026-10-27T11:26:18"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 11.30
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-g1tB
---

The four roles of the consultants council are the planner, researcher, critic, and synthesizer (`consultants/engine/council.py:98`), each with its own system prompt (`PLANNER_SYSTEM` at `consultants/engine/council.py:116`, `RESEARCHER_SYSTEM` at `consultants/engine/council.py:126`, and the critic and synthesizer prompts further down in the same file).
