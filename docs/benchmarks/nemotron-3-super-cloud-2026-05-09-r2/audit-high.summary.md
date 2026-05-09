---
session_id: csl-2026-05-09-0745-a436
created: "2026-05-09T07:45:13"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "nemotron-3-super:cloud"
  planner: "nemotron-3-super:cloud"
  researcher: "nemotron-3-super:cloud"
  synthesizer: "nemotron-3-super:cloud"
topology: council
effort: high
duration_seconds: 458.01
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-LQZY
---

The consultation ends with `status=failed` but the synthesizer can still return a coherent (degraded) answer that lacks the insights from the failed researcher lane.  

The exception in `agent_loop.runner.run_loop` is caught by `researcher_node`, which returns a tombstone update containing the error string in `research` and `turns` and sets `error`/`_role_failed` (consultants/engine/council.py:563‑565). CouncilState’s additive reducers merge this error text and the turn into the overall state (consultants/engine/graph.py:60‑73), while the `error` and `_role_failed` fields lack reducers, so parallel lane errors overwrite each other via last‑write‑wins (consultants/engine/graph.py:74‑75). After the graph streams, the runner’s loop sets `terminal_status = "failed"` when `final_state.get("error")` is truthy (consultants/server/runner.py:172‑174). The synthesizer still runs and receives the `research` list that includes the error strings, allowing it to craft a final answer missing the failed lane’s insights (consultants/server/runner.py:186,199). Artifacts are written unconditionally via `storage.write_consultation`, preserving whatever `final_answer` the synthesizer produced (storage.py:200‑207). Thus the user sees a failed consultation but may obtain a degraded, incomplete answer.  

Hardening: add the lane index to the researcher lane error message to improve debugging (consultants/engine/council.py:562 → `err_text = f"(researcher lane {state.get('lane_idx', '?')} failed: {e})"`).
