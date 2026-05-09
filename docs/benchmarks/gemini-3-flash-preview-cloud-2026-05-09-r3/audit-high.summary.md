---
session_id: csl-2026-05-09-0807-e502
created: "2026-05-09T08:07:12"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemini-3-flash-preview:cloud"
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemini-3-flash-preview:cloud"
topology: council
effort: high
duration_seconds: 76.24
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-qba3
---

The council produces a **degraded but coherent answer**, but the consultation is ultimately marked with **`status=failed`** in metadata. Because the researcher node internally catches exceptions, the graph superstep completes and allows the synthesizer to run using partial data.

### Failure Path Trace

1.  **CouncilState's additive reducers (`consultants/engine/graph.py:64, 68`):** The researcher lane fanned out via `Send` returns a state update containing an error tombstone. The `operator.add` reducer concatenates this string into the global `research` list rather than dropping the lane's contribution.
2.  **`consultants/server/runner.py`'s `compiled.stream` loop (`runner.py:137-141`):** The `researcher_node` handles the exception internally (`consultants/engine/council.py:555`) and returns a dict. Consequently, the stream generator does not raise an exception; the `except` block in the runner (`runner.py:155`) is not triggered, and the graph proceeds to the synthesizer.
3.  **The synthesizer's input messages (`consultants/engine/council.py:718`):** The `synthesizer_node` calls `build_synthesizer_messages`, which iterates over the `research` list. It sees the error message — e.g., `(researcher lane failed: ...)` — as a valid piece of "evidence" (`consultants/engine/council.py:254`). The LLM synthesizes a response using findings from successful lanes while acknowledging the failure as an "uncertainty" or "missing data."
4.  **The `storage.py` artifact writer (`runner.py:172-199`):** After the stream finishes, the runner inspects the final state. Finding the `error` key populated by the failed lane (`council.py:564`), it sets `terminal_status = "failed"` (`runner.py:174`). This status is persisted to `metadata.json` and the `summary.md` YAML front-matter via `storage.write_consultation` (`consultants/engine/storage.py:200`).

### Perception
The user receives a completed `summary.md` with a logical answer based on surviving researcher lanes. However, the system-level status is `failed`, and the synthesis may explicitly mention the researcher's failure if the LLM interprets the tombstone as a significant gap in the investigation.

### Recommended Hardening
Coerce `tool_executor` outputs to strings to prevent `TypeError` exceptions within the agent loop when tools return non-string types (e.g., `None` or `dict`).

```python
# agent_loop/runner.py:122
-             output = tool_executor(name, args_str, cwd)
+             output = str(tool_executor(name, args_str, cwd))
```
