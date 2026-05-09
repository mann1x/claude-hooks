---
session_id: csl-2026-05-09-0918-0578
created: "2026-05-09T09:18:08"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 111.64
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-HJf4
---

The council produces a coherent but degraded answer; the session is marked as `status=failed` in metadata, but a final response is still delivered to the user.

*   **CouncilState Reducers**: The `researcher_node` catches the exception internally (`consultants/engine/council.py:556-573`) and returns a "tombstone" update. Because `CouncilState.research` and `CouncilState.turns` use `operator.add` reducers (`consultants/engine/graph.py:64, 68`), the error string `"(researcher lane failed: ...)"` is concatenated into the global state alongside successful reports.
*   **Runner Stream Loop**: The exception does not propagate to `consultants/server/runner.py:137` because it is handled inside the node. The `compiled.stream` loop continues normally, allowing the graph to proceed to the synthesizer.
*   **Synthesizer Input**: The synthesizer's prompt is constructed by iterating over the `research` list (`consultants/engine/council.py:253`). It receives the successful findings and the literal error string, treating the latter as a piece of context.
*   **Artifact Writer**: The runner detects the `error` key set by the failed researcher node (`consultants/engine/council.py:564`) and assigns `terminal_status = "failed"` (`consultants/server/runner.py:174`). This status is persisted via `storage.write_consultation` (`consultants/server/runner.py:189`), while the `summary.md` is still written with the synthesizer's best-effort response (`consultants/engine/storage.py:119`).

**Failure Mode**: The user perceives a "confidently degraded" answer. They receive a final response that may be missing critical information, and while the metadata indicates a failure, the answer text itself typically ignores the lane failure unless the LLM happens to mention the error string.

**Recommended Hardening**:
Update `SYNTHESIZER_SYSTEM` in `consultants/engine/council.py:156` to include: `"If any researcher reports contain failure markers (e.g., 'lane failed'), explicitly state in your final answer which specific part of the plan could not be verified."`
