---
session_id: csl-2026-10-22-2016-3c35
created: "2026-10-22T20:16:51"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 28.35
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-CRcf
---

The council produces a degraded but coherent answer. The entire consultation does not crash; instead, it completes the graph execution and marks the final artifact as `status=failed` in the metadata.

**Failure Path Trace:**

1.  **CouncilState's Additive Reducers**: Exceptions in `run_loop` are caught by a `try...except` block in `researcher_node` (`consultants/engine/council.py:547-555`). The node returns a "tombstone" dictionary containing an error message (`consultants/engine/council.py:562-573`). Because `CouncilState` uses `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), the failure string `"(researcher lane failed: <exception>)"` is appended to the research list, and a corresponding `RoleTurn` is added to the transcript.
2.  **`compiled.stream` Loop**: Since the exception is handled within the node, the stream loop in `consultants/server/runner.py:137-146` does not encounter an unhandled exception and continues to the next superstep. The process completes normally, and `final_state` (`consultants/server/runner.py:140`) captures the merged state including the tombstone.
3.  **Synthesizer's Input**: The synthesizer receives the merged research list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees all successful reports plus the explicit failure string `"(researcher lane failed: <exception>)"` as part of the evidence (`consultants/engine/council.py:253-254`).
4.  **Storage Artifact Writer**: The server detects the error in `final_state.get("error")` (`consultants/server/runner.py:172`) and sets `terminal_status = "failed"` (`consultants/server/runner.py:174`). `storage.write_consultation` (`consultants/engine/storage.py:200`) then writes the synthesizer's (potentially degraded) answer to `summary.md`, but includes `status: failed` and the error message in the YAML front-matter (`consultants/engine/storage.py:108-110`).

**User Perception:**
The user receives a final answer that is missing information from the failed researcher lane. The metadata/header of the result indicates `status: failed`, but the content remains a coherent response based on the surviving lanes.

**Recommended Hardening:**
Update `consultants/engine/council.py:562` to use a more prominent error marker (e.g., `!!! SYSTEM ERROR: Researcher Lane Failed !!!\nReason: {e}`) to ensure the synthesizer recognizes the gap as a system failure rather than a lack of available evidence.
