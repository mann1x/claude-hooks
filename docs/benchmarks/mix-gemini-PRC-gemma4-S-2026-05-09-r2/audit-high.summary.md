---
session_id: csl-2026-05-09-1021-0736
created: "2026-05-09T10:21:46"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemini-3-flash-preview:cloud"
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 126.85
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-CvAB
---

The council produces a coherent but degraded answer and surfaces as `status=failed`.

*   **CouncilState Reducers**: The `research` field uses an additive reducer (`Annotated[list, operator.add]`) at `consultants/engine/graph.py:64`. Parallel lane outputs are concatenated; if one lane fails, its "tombstone" error message is simply appended to the list of successful findings.
*   **Runner Stream Loop**: The `compiled.stream` loop at `consultants/server/runner.py:137` does not terminate because the `researcher_node` catches all exceptions within a `try...except` block at `consultants/engine/council.py:555`, preventing the exception from propagating to the orchestrator.
*   **Synthesizer Input**: The `synthesizer_node` processes the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:253`). The synthesizer sees the literal string `(researcher lane failed: ...)` (`consultants/engine/council.py:566`) as a research finding and incorporates it into the final response.
*   **Storage Artifact Writer**: After the stream completes, `consultants/server/runner.py:172-174` detects the `error` key in the final state and sets the session status to `failed`. `consultants/engine/storage.py:108-110` then writes `status: failed` into the `summary.md` YAML front-matter and `metadata.json`, while the file body contains the degraded answer.

**Perceived Failure Mode**: The user receives a formatted answer that is likely incomplete (missing data from the failed lane), but the session metadata/UI explicitly indicates a failure.

**Hardening Recommendation**: Wrap tool execution results in a string cast to prevent `TypeError` exceptions when a tool returns a non-string value, avoiding the failure path entirely.
*   **Change**: `output = str(tool_executor(name, args_str, cwd))` at `claude_hooks/agent_loop/runner.py:122`.
