---
session_id: csl-2026-10-22-1814-94cc
created: "2026-10-22T18:14:54"
question: From the code in this repository, name the four roles of the consultants council in a single short sentence.
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 53.70
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-6kcB
---

The council's four roles are the **planner**, who decomposes the question into 3-7 concrete investigation steps (`consultants/engine/council.py:116`); the **researcher**, who executes that plan with the project's file/search tools and reports findings with `path:line` citations (`consultants/engine/council.py:126`); the **critic**, who judges whether the researcher's evidence is sufficient and returns `DECISION: ready` or `DECISION: needs_more_research` (`consultants/engine/council.py:144`); and the **synthesizer**, who writes the final user-facing answer from the plan, report(s), and verdict (`consultants/engine/council.py:156`).

The canonical set is `ROLES = ("planner", "researcher", "critic", "synthesizer")` (`consultants/config.py:40`), with only the synthesizer mandatory (`consultants/config.py:41`, `consultants/engine/graph.py:20`); the pipeline runs planner → researcher → critic → synthesizer, with the critic able to loop back to the researcher for another round (`consultants/engine/graph.py:13`, `consultants/engine/graph.py:17`).
