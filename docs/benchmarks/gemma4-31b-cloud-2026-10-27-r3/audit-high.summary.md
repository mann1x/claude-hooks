---
session_id: csl-2026-10-27-1209-846b
created: "2026-10-27T12:09:21"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 43.24
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-iMej
---

The council produces a degraded but coherent answer, although the consultation is marked as `status=failed` in the metadata.

*   **CouncilState Reducers**: An exception in `agent_loop.runner.run_loop` is caught by the `researcher_node` wrapper (`consultants/engine/council.py:547-555`). The node returns a "tombstone" update which uses additive reducers in `CouncilState` (`consultants/engine/graph.py:64, 68`) to append the error string `(researcher lane failed: <exception>)` to the `research` list and a failure `RoleTurn` to the `turns` list.
*   **Server Stream Loop**: The `compiled.stream` loop (`consultants/server/runner.py:137`) does not crash because the exception was swallowed. It continues to update `final_state` via the `"values"` stream mode (`consultants/server/runner.py:139-141`).
*   **Synthesizer Input**: The synthesizer receives the aggregated research list via `build_synthesizer_messages` (`consultants/engine/council.py:245-267`). It sees the tombstone error string as a research report entry (`consultants/engine/council.py:238`) and generates a `final_answer` using the surviving lanes.
*   **Storage Artifact Writer**: The server detects the error in `final_state` (`consultants/server/runner.py:172`) and sets `terminal_status = "failed"` (`consultants/server/runner.py:174`). `storage.write_consultation` (`consultants/server/runner.py:199`) then persists the degraded `final_answer` to `summary.md` (`consultants/engine/storage.py:119`) while marking the `metadata.json` as `status: "failed"`.

**User Perception**: The user receives a response that may be incomplete or explicitly mention research failures, but the internal system logs and artifacts record the session as a failure.

**Hardening Recommendation**: Modify `consultants/server/runner.py:174` to prevent researcher-lane failures from marking the entire consultation as failed when a final answer is still produced.
**Change**: Replace `terminal_status = "failed" if node_error else "completed"` with `terminal_status = "failed" if (node_error and final_state.get("_role_failed") == "synthesizer") else "completed"`.
