---
session_id: csl-2026-10-22-1916-60d8
created: "2026-10-22T19:16:49"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 48.56
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-tQts
---

The council produces a degraded but coherent answer; the overall consultation is marked as `status=failed`, but the synthesizer still generates a response based on surviving researcher lanes.

**Failure Path Trace:**
*   **Exception Catching:** An unhandled exception in `agent_loop.runner.run_loop` is caught by the `researcher_node` wrapper at `consultants/engine/council.py:547`. This prevents the entire graph from crashing.
*   **Tombstone Generation:** The node returns a "tombstone" state update containing an error string `(researcher lane failed: <exception>)` at `consultants/engine/council.py:562`, a global error flag at `consultants/engine/council.py:564`, and the failure text added to the research list at `consultants/engine/council.py:568`.
*   **Additive Reducers:** In `consultants/engine/graph.py:64` and `consultants/engine/graph.py:68`, the `CouncilState` uses `operator.add` for `research` and `turns`. This merges the failure tombstone into the global state, resulting in a `research` list that contains a mix of successful findings and error strings.
*   **Server Stream Loop:** The `compiled.stream` loop in `consultants/server/runner.py:137` continues to run because the exception was handled within the node. After the stream completes, the runner checks `final_state.get("error")` at `consultants/server/runner.py:172` and sets `terminal_status = "failed"` at `consultants/server/runner.py:174`.
*   **Synthesizer Input:** The `build_synthesizer_messages` function iterates over the merged research list at `consultants/engine/council.py:254`. The synthesizer receives the literal string `(researcher lane failed: <exception>)` as if it were a piece of research evidence.
*   **Artifact Writer:** `consultants/server/runner.py:199` calls `storage.write_consultation`, which executes the write at `consultants/engine/storage.py:207`. The `metadata.json` file records the `status: "failed"` and the error string via `asdict(result)` at `consultants/engine/storage.py:168`, while `summary.md` contains the degraded answer.

**User Perception:**
The user receives a coherent response that appears successful but may be missing information. The failure is only visible if the user inspects the `metadata.json` or if the synthesizer explicitly mentions the failed lane in its response.

**Recommended Hardening:**
In `consultants/engine/council.py:562`, change the tombstone text to use a structured tag to prevent the synthesizer from interpreting the error as a factual finding:
`err_text = f"[[LANE_ERROR]]: Researcher lane failed to provide findings due to: {e}"`
