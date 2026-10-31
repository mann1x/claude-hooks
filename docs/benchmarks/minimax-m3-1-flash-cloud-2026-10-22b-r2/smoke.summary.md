---
session_id: csl-2026-10-22-1635-032a
created: "2026-10-22T16:35:28"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 36.64
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-NShH
---

The consultants council has exactly four roles — `planner`, `researcher`, `critic`, `synthesizer` (`consultants/config.py:40`) — where the **planner** decomposes the question into 3–7 concrete investigation steps (`consultants/engine/council.py:117`), the **researcher** executes that plan with project tools and cites findings as `path:line` (`consultants/engine/council.py:127`), the **critic** judges whether the evidence is sufficient and returns `DECISION: ready` or `DECISION: needs_more_research` (`consultants/engine/council.py:145`), and the **synthesizer** writes the final user-facing answer from the plan, report(s), and verdict (`consultants/engine/council.py:157`).
