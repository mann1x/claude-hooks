---
session_id: csl-2026-05-07-1006-b8a8
created: "2026-05-07T10:06:13"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m2.7:cloud"
  researcher: "minimax-m2.7:cloud"
  synthesizer: "minimax-m2.7:cloud"
topology: council
effort: medium
duration_seconds: 35.84
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-NhRc
---

The four roles are: **planner** decomposes questions into steps, **researcher** executes them with project tools and cites findings, **critic** judges whether evidence is sufficient, and **synthesizer** writes the final answer the user sees — citing `path:line` for every codebase claim (`consultants/engine/council.py:116,126,144,156`).
