---
session_id: csl-2026-05-09-0650-4b7d
created: "2026-05-09T06:50:05"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "qwen3-coder-next:cloud"
  researcher: "qwen3-coder-next:cloud"
  synthesizer: "qwen3-coder-next:cloud"
topology: council
effort: medium
duration_seconds: 18.13
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-KnHx
---

The four roles of the consultants council are planner (decomposes questions into concrete investigation steps at `consultants/engine/council.py:116`), researcher (executes plans using project tools and cites `path:line` at `consultants/engine/council.py:126`), critic (assesses evidence sufficiency and decides `ready` or `needs_more_research` at `consultants/engine/council.py:144`), and synthesizer (writes the final user-facing answer, citing `path:line` for codebase claims at `consultants/engine/council.py:156`).
