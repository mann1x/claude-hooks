---
session_id: csl-2026-10-22-1615-b76c
created: "2026-10-22T16:15:04"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 34.66
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-PPRS
---

The consultants council is a four-role LLM pipeline (`consultants/config.py:40`) — a **planner** that decomposes the question into 3-7 concrete investigation steps (`consultants/engine/council.py:116`), a **researcher** that executes that plan with project tools and reports `path:line`-grounded findings (`consultants/engine/council.py:126`), a **critic** that rules the evidence `ready` or `needs_more_research` (`consultants/engine/council.py:144 [no needs_more_research at this line; module scope]`), and a **synthesizer** that turns plan, reports, and verdict into the final bottom-line answer (`consultants/engine/council.py:156`).
