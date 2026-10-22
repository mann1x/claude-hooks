---
session_id: csl-2026-10-22-1839-883f
created: "2026-10-22T18:39:52"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 63.13
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rb5U
---

The council produces a degraded but coherent answer, but the consultation is marked as `status=failed`.

**Failure Path Trace**
*   **Exception Handling**: An exception in the runner loop (called at `consultants/engine/council.py:548`) is caught by the `try...except` block at `consultants/engine/council.py:547-555`. The `researcher_node` then returns a "tombstone" update at `consultants/engine/council.py:563-573` containing the error string in the `research` list and setting `error` and `_role_failed` in the state.
*   **Additive Reducers**: Because `CouncilState.research` (`consultants/engine/graph.py:64`) and `CouncilState.turns` (`consultants/engine/graph.py:68`) use `operator.add`, the tombstone is appended to the global state. Results from other parallel lanes are preserved.
*   **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures this merged state into `final_state`. Since the error was handled inside the node, the graph execution continues normally.
*   **Synthesizer Input**: The synthesizer calls `build_synthesizer_messages` at `consultants/engine/council.py:719-721`, which iterates over the `research` list at `consultants/engine/council.py:253-254 [no research at this line; line is in build_synthesizer_messages]`. The synthesizer receives a mix of successful reports and the literal string `"(researcher lane failed: ...)"`.
*   **Artifact Writer**: The runner checks `final_state.get("error")` at `consultants/server/runner.py:172`. Because the tombstone set this field, `terminal_status` is set to `"failed"` at `consultants/server/runner.py:174`, which is then written as the final `status` of the `ConsultationResult` at `consultants/server/runner.py:189 [no ConsultationResult at this line; line is in run_council]`.

**User Perception**
The user receives a final answer synthesized from the remaining healthy researcher lanes. However, the answer is degraded (missing evidence from the failed lane), and the overall API response/artifact indicates a failure (`status=failed`).

**Hardening Recommendation**
Modify `consultants/server/runner.py:174` to:
`terminal_status = "failed" if (node_error and node_failed == "synthesizer") else "completed"`
