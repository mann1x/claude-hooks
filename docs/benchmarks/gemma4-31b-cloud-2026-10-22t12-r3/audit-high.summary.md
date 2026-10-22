---
session_id: csl-2026-10-22-1904-3e4c
created: "2026-10-22T19:04:13"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 90.48
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-6CJQ
---

The council produces a degraded answer, but the entire consultation surfaces as `status=failed`.

*   **CouncilState's additive reducers**: The `research` key is defined with `Annotated[list, operator.add]` in `consultants/engine/graph.py:64`. When a researcher lane fails, the `researcher_node` catches the exception at `consultants/engine/council.py:555` and returns a tombstone list containing the error string `(researcher lane failed: ...)`. This string is appended to the global `research` list via the reducer.
*   **`consultants/server/runner.py`'s compiled.stream loop**: The loop (`consultants/server/runner.py:137-141`) finishes normally without raising an exception because the error was handled inside the node and returned as a state update.
*   **Synthesizer's input**: The `build_synthesizer_messages` function iterates over research rounds at `consultants/engine/council.py:253-254`, inserting the failure string directly into the prompt as a research report. The synthesizer synthesizes the final answer using only the surviving reports.
*   **The storage.py artifact writer**: The runner checks `final_state.get("error")` at `consultants/server/runner.py:172`. Because the tombstone populated this key at `consultants/engine/council.py:564`, the runner sets `terminal_status = "failed"` at `consultants/server/runner.py:174` and persists this failed status via `storage.write_consultation` at `consultants/server/runner.py:189-199`.

**Failure Mode**: The user perceives a system failure (via the `status` and `error` fields in the consultation metadata), even though a partially correct `final_answer` is still generated and written to `summary.md`.

**Recommended Hardening**:
Remove the `"error"` key from the `researcher_node` failure return to prevent a single lane failure from marking the entire consultation as failed.
`consultants/engine/council.py:564` (Remove `"error": f"researcher failed: {e}",`)
