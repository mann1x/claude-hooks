---
session_id: csl-2026-05-09-1010-8a10
created: "2026-05-09T10:10:49"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemini-3-flash-preview:cloud"
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 120.40
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-KaZP
---

The council produces a coherent but degraded answer, though the consultation surfaces as `status=failed` in metadata.

*   **CouncilState Reducers**: The `research` and `turns` keys use `operator.add` (`consultants/engine/graph.py:64, 68`). When a lane fails, `researcher_node` catches the exception (`consultants/engine/council.py:555`) and returns a "tombstone" update containing an error string. This string is appended to the global research list via the additive reducer.
*   **Compiled Stream Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` does not terminate because the `researcher_node` returns a valid state update instead of raising a graph-level exception. The loop continues to the synthesizer node.
*   **Synthesizer Input**: The synthesizer's prompt-builder iterates over the merged `research` list (`consultants/engine/council.py:253-254`), passing the tombstone error message (e.g., `"(researcher lane failed: ...)"`) into the LLM context as if it were a research report.
*   **Storage Artifact Writer**: The `runner.py` loop detects the `error` key in the final state (`consultants/server/runner.py:174`) and sets `terminal_status = "failed"`. `storage.py` persists this status in `metadata.json` (`consultants/engine/storage.py:108-110`), while still writing the synthesizer's output to `summary.md`.

**Perceived Failure Mode**: The user sees a "failed" status in the session history/UI, but the actual output (`summary.md`) contains a response that is missing data from the failed lane and may explicitly reference the technical error in its reasoning.

**Hardening Recommendation**:
Coerce tool executor output to a string in the agent loop to prevent `TypeError` or serialization crashes when a tool returns a non-string object.
`claude_hooks/agent_loop/runner.py:133`: change `"content": output` to `"content": str(output)`
