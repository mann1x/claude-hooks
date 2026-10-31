---
session_id: csl-2026-10-22-1953-0475
created: "2026-10-22T19:53:00"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 34.68
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-EirC
---

The council produces a coherent but degraded answer, though the consultation metadata is marked as `status=failed`.

**Failure Path Trace:**

*   **Exception Capture**: An unhandled exception in `agent_loop.runner.run_loop` propagates to `researcher_node` (`consultants/engine/council.py:547`), where it is caught in a `try...except` block (`consultants/engine/council.py:547-555`). The node returns a "tombstone" state update containing a failure string `(researcher lane failed: <exception>)` in the `research` list and sets `_role_failed` to `"researcher"` (`consultants/engine/council.py:563-573`).
*   **CouncilState Reducers**: Because the researcher lanes are fanned out via the Send API, the results are merged into the global state. The `research` and `turns` channels use `operator.add` (`consultants/engine/graph.py:64, 68`), meaning the tombstone failure string is concatenated into the state alongside reports from surviving lanes.
*   **Server Stream Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` continues to execute. Since the exception was handled inside the node, it does not trigger the outer exception handler (`consultants/server/runner.py:155`), allowing the graph to proceed to the synthesizer.
*   **Synthesizer Input**: The synthesizer constructs its prompt by iterating over the merged `research` list (`consultants/engine/council.py:253-254 [no research at this line; line is in build_synthesizer_messages]`). It receives the failure string `(researcher lane failed: ...)` as a legitimate research report and attempts to synthesize an answer based on the remaining available evidence.
*   **Artifact Writer**: In `consultants/server/runner.py:172`, the runner checks `final_state.get("error")`. Because the tombstone update populated this key, `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`). However, `storage.write_consultation` is still called, persisting the (potentially degraded) `final_answer` and the failure status (`consultants/server/runner.py:177-199`).

**User Perception**: The user receives a functional response, but the answer may be incomplete or the model may hallucinate to fill the gap left by the failed researcher. The API response will indicate a failure status despite providing an answer.

**Recommended Hardening**:
Include the specific `plan_item` in the tombstone error string so the synthesizer can explicitly identify and report which part of the investigation failed.

`consultants/engine/council.py:562`:
`- err_text = f"(researcher lane failed: {e})"`
`+ err_text = f"(researcher lane failed while investigating {state.get('plan_item', 'unknown')}: {e})"`
