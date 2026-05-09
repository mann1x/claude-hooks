---
session_id: csl-2026-05-09-0731-3531
created: "2026-05-09T07:31:31"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemini-3-flash-preview:cloud"
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemini-3-flash-preview:cloud"
topology: council
effort: high
duration_seconds: 110.55
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-VC2D
---

The council produces a **degraded answer** while the consultation metadata is marked as **`status=failed`**.

*   **CouncilState Reducers**: The `researcher_node` catches the exception (`consultants/engine/council.py:555`) and returns a "tombstone" dictionary (`consultants/engine/council.py:563-573`). Because `CouncilState` defines the `research` and `turns` channels using `Annotated[list, operator.add]` (`consultants/engine/graph.py:64, 68`), the failure notification string `"(researcher lane failed: ...)"` is concatenated into the shared state alongside results from successful parallel lanes.
*   **Stream Loop**: In `consultants/server/runner.py:137-141`, the `compiled.stream` loop continues to drain. The graph does not terminate early because the node handled the exception internally and returned a valid (though error-laden) state update. 
*   **Synthesizer Input**: The `synthesizer_node` calls `build_synthesizer_messages` (`consultants/engine/council.py:245-266`), which iterates through the `research` list. The synthesizer receives the tombstone failure message as if it were a valid research report (line 254) and attempts to incorporate this "missing data" notice into its final summary.
*   **Storage Writer**: After the graph completes, the server runner extracts the `error` key from the final state (`consultants/server/runner.py:172`). Finding the tombstone error, it sets `terminal_status = "failed"` (line 174). `storage.write_consultation` (`consultants/server/runner.py:199`) persists the artifact with this failed status.

**User Perception**: The user sees a coherent summary in the UI (the "degraded" answer), but the consultation is flagged with a "failed" status indicator. The final report will likely contain explicit mentions of the researcher lane failure within the text.

**Recommended Hardening Change**:
Coerce tool executor outputs to strings in the tracer to prevent `TypeError` when `len()` is called on non-string returns (e.g., integers).
`consultants/engine/trace.py:230`
```python
- output_chars=len(out or ""),
+ output_chars=len(str(out) if out is not None else ""),
```
