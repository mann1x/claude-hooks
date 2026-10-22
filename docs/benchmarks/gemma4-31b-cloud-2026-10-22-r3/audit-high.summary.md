---
session_id: csl-2026-10-22-1300-1466
created: "2026-10-22T13:00:19"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 64.27
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Dxkr
---

The council produces a degraded but coherent answer; the consultation does not crash, but it is marked as `status=failed` in the metadata.

### Failure Path Trace

1.  **CouncilState's Additive Reducers**: The `researcher_node` wraps the loop execution in a `try...except` block (`consultants/engine/council.py:547-573`). Upon an exception, it returns a "tombstone" dictionary. Because `CouncilState.research` and `CouncilState.turns` use `operator.add` as reducers (`consultants/engine/graph.py:64, 68`), the error string `"(researcher lane failed: {e})"` and the failure `RoleTurn` are appended to the global state without interrupting other parallel lanes.
2.  **Server Loop**: The `compiled.stream` loop (`consultants/server/runner.py:136-161`) does not crash because the exception was neutralized within the node. The loop continues to completion, and `final_state` (`consultants/server/runner.py:140`) captures the merged state including the tombstone.
3.  **Synthesizer's Input**: The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees the successful reports from surviving lanes interleaved with the literal error string `"(researcher lane failed: ...)"` (`consultants/engine/council.py:253-254`), treating the failure as a textual finding.
4.  **Storage Artifact Writer**: The runner checks `final_state.get("error")` to determine the session status (`consultants/server/runner.py:172 [no failed at this line; line is in run_council]-174`). Since the tombstone set the `error` key (`consultants/engine/council.py:564`), `terminal_status` is set to `"failed"`. This status and the error message are written to the `summary.md` YAML front-matter (`consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`) and the `transcript.md`.

**User Perception**: The user receives a functional answer based on the remaining researchers. The failure is only visible if the user inspects the `status: failed` metadata in the summary or the specific error in the transcript.

### Recommended Hardening
Prevent a single researcher lane failure from marking the entire consultation as failed if a final answer was still produced.

**Change**: Modify the terminal status logic to only mark the session as `failed` if the synthesizer fails or no final answer is present.
**Path**: `consultants/server/runner.py:172 [no failed at this line; line is in run_council]`
**Implementation**:
```python
node_error = final_state.get("error") if final_state.get("_role_failed") == "synthesizer" else None
```
