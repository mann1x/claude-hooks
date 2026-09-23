---
session_id: csl-2026-09-23-2016-9dc3
created: "2026-09-23T20:16:55"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 12.13
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-l2Px
---

The four roles of the consultants council are **planner, researcher, critic, and synthesizer** — defined as `ROLES = ("planner", "researcher", "critic", "synthesizer")` in `consultants/config.py:40` and corroborated by the pipeline order in `consultants/engine/graph.py:202-203` and the assertion in `tests/test_consultants_config.py:27`.
