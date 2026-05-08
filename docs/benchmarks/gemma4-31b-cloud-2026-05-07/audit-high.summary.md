---
session_id: csl-2026-05-07-0952-b0fc
created: "2026-05-07T09:52:49"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 181.91
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-s7IY
---

The council produces a degraded but coherent answer, but the overall consultation is marked as `status=failed`.

*   **CouncilState Reducers**: The exception is caught at `consultants/engine/council.py:555`, and the node returns a "tombstone" update containing an error string in the `research` list (`consultants/engine/council.py:566`). Because `CouncilState.research` uses `operator.add` (`consultants/engine/graph.py:64`), this error string is appended to the successful reports from other parallel lanes.
*   **Runner Stream Loop**: The `compiled.stream` loop (`consultants/server/runner.py:137`) does not crash. However, the runner checks for the presence of the `error` key in the final state (`consultants/server/runner.py:172`), which triggers `terminal_status = "failed"` (`consultants/server/runner.py:174`).
*   **Synthesizer Input**: The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245`). It sees the valid reports from successful lanes and the failure string (e.g., `"(researcher lane failed: ...)"`) for the crashed lane, attempting to synthesize an answer from the remaining evidence.
*   **Artifact Writer**: The `storage.py` writer persists the `status="failed"` marker into `metadata.json` (`consultants/engine/storage.py:168`) and the `summary.md` YAML front-matter (`consultants/engine/storage.py:108`), even though a `final_answer` was generated.

**Failure Mode**: Silent degradation. The user receives a response that appears successful in the UI/body but is flagged as a system failure in the metadata. The synthesizer may hallucinate or omit facts due to the missing research dimension without being explicitly told the lane failed.

**Recommended Hardening**:
Modify `build_synthesizer_messages` in `consultants/engine/council.py:245` to check for the `error` key in the state and explicitly append a warning to the synthesizer's prompt, ensuring it acknowledges the missing evidence rather than ignoring the failure string.
