---
session_id: csl-2026-10-27-1115-0a04
created: "2026-10-27T11:15:31"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 37.84
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-FFQy
---

The council produces a degraded answer rather than failing entirely; the user perceives a coherent but incomplete response, while the session metadata is marked as `status=failed`.

**Failure Path Trace:**
*   **Exception Origin**: An unhandled exception in `agent_loop.runner.run_loop` propagates to the `researcher_node` in `consultants/engine/council.py:555`, where it is caught by a generic `except Exception` block.
*   **Tombstone Generation**: The `researcher_node` returns a "tombstone" update containing an error string `"(researcher lane failed: {e})"` in the `research` list (`consultants/engine/council.py:566`), a `RoleTurn` with the error text (`consultants/engine/council.py:568-573`), and a populated `error` field (`consultants/engine/council.py:564`).
*   **State Reduction**: `CouncilState` uses `operator.add` to merge `research` and `turns` (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), appending the failure tombstone to the results of healthy lanes.
*   **Execution Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` continues normally because the exception was handled within the node. However, since `final_state.get("error")` is now populated, the runner sets `terminal_status = "failed"` (`consultants/server/runner.py:172-174`).
*   **Synthesizer Input**: The `build_synthesizer_messages` function passes the merged `research` list to the synthesizer (`consultants/engine/council.py:245 [no research at this line; line is in build_synthesizer_messages]`), meaning the LLM receives the literal string `"(researcher lane failed: ...)"` as one of its research reports (`consultants/engine/council.py:254`).
*   **Artifact Writing**: `consultants/server/runner.py:189, 199` triggers `storage.write_consultation` with `status="failed"`, but it still persists the `final_answer` produced by the synthesizer.

**Recommended Hardening:**
Update `consultants/engine/council.py:562 [no final_answer at this line; line is in researcher_node]` to use a structured marker to prevent the synthesizer from treating the error as valid research content:
`err_text = f"### LANE_FAILURE: {e}"`
