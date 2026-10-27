---
session_id: csl-2026-10-27-1616-2906
created: "2026-10-27T16:16:59"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 78.34
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-OCdZ
---

The council produces a degraded but coherent answer, but the user perceives the entire consultation as `status=failed`.

**Failure Path Trace:**
1.  **`researcher_node` Catch:** An exception in `agent_loop.runner.run_loop` (e.g., HTTP timeout or `TypeError` from a non-string tool output) is caught by the broad `try...except Exception` block in `consultants/engine/council.py:555-573`. The node returns a "tombstone" delta: `{"error": "researcher failed: <e>", "_role_failed": "researcher", "research": ["(researcher lane failed: <e>)"]}`.
2.  **`CouncilState` Reducers:** 
    *   The `research` and `turns` channels use `operator.add` (`consultants/engine/graph.py:64, 68`), so the tombstone's failure string and empty turn are concatenated with the results of successful lanes.
    *   The `error` and `_role_failed` channels are plain (last-write-wins) (`consultants/engine/graph.py:74-75`). A single failing lane sets these values; multiple failing lanes may lead to non-deterministic error text or a graph crash due to concurrent writes to a non-reducer channel.
3.  **`compiled.stream` Loop:** Because the exception was caught and converted to a state update, the `compiled.stream` loop in `consultants/server/runner.py:137-154` completes normally. The `except Exception` block at `runner.py:155-161` is not triggered.
4.  **Synthesizer Input:** `build_synthesizer_messages` (`consultants/engine/council.py:245-266`) iterates over the `research` list. The synthesizer sees the tombstone string `(researcher lane failed: <exc repr>)` as a report body (`consultants/engine/council.py:253-254`). It then synthesizes a final answer based on the remaining successful lanes, potentially hedging or ignoring the gap.
5.  **Storage and Status:**
    *   The runner checks `final_state.get("error")` at `consultants/server/runner.py:172`.
    *   Because the tombstone populated `error`, `terminal_status` is set to `"failed"` at `consultants/server/runner.py:174`.
    *   `storage.write_consultation` (`consultants/engine/storage.py:200-208`) writes the full synthesized answer to `summary.md` (`storage.py:117-120`), but sets the YAML front-matter `status` to `failed` (`storage.py:108-110`).
    *   The user sees `status: "failed"` and the error message via the API endpoint (`consultants/server/app.py:61-71`).

**User Perception:** The user receives a "Failed" status via the API/metadata, but if they open `summary.md`, they find a substantive (though potentially incomplete) answer.

**Recommended Hardening Change:**
Modify `consultants/server/runner.py:174` to prevent researcher-level failures from poisoning the terminal status of a consultation that successfully produced a final answer:
```python
terminal_status = "failed" if (node_error and (final_state.get("_role_failed") != "researcher" or not final_state.get("final_answer"))) else "completed"
```
