---
session_id: csl-2026-05-09-0615-63b9
created: "2026-05-09T06:15:52"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemini-3-flash-preview:cloud"
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemini-3-flash-preview:cloud"
topology: council
effort: high
duration_seconds: 127.25
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-jYrZ
---

The council produces a **coherent but degraded answer** while the consultation's terminal status is marked as **failed**.

*   **CouncilState Reducers**: The `research` list uses `Annotated[list, operator.add]` (`consultants/engine/graph.py:64`), meaning the failure notice from the crashed lane is appended alongside successful reports from other lanes. However, the `error` and `_role_failed` keys have no reducers (`consultants/engine/graph.py:74-75`) and use default overwrite; the final state preserves only the error from the last lane to fail.
*   **Stream Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` does not crash because `researcher_node` wraps the agent execution in a `try/except` block (`consultants/engine/council.py:555`). It catches the exception and returns a "tombstone" state containing the error message and a failure string in the `research` list (`consultants/engine/council.py:562-573`).
*   **Synthesizer Input**: The synthesizer's message builder iterates over the entire `research` list (`consultants/engine/council.py:253-254`). It treats the failure tombstone (e.g., `"(researcher lane failed: ...)"`) as a literal research report. The synthesizer then generates a response based on the surviving lanes' data and the explicit notification of the failure.
*   **Storage**: After the graph completes, the runner detects the `error` key in the final state and sets `terminal_status = "failed"` (`consultants/server/runner.py:172-174`). `storage.write_consultation` persists this status in `metadata.json` and the YAML front-matter of `summary.md` (`consultants/engine/storage.py:108-110, 200`), even though the `final_answer` text is successfully written to the file (`consultants/engine/storage.py:117-121`).

**Failure Mode**: The user sees a completed `summary.md` containing a functional answer that may explicitly mention a lane failure or omit expected data. However, any UI or API polling the consultation status will report it as `failed`.

**Hardening Change**: Change the `research_rounds_used` reducer from `operator.add` to `max` to prevent parallel fan-out lanes from incorrectly multiplying the round count and prematurely exhausting the consultation's turn budget.

`consultants/engine/graph.py:69`:
```python
research_rounds_used: Annotated[int, max]
```
