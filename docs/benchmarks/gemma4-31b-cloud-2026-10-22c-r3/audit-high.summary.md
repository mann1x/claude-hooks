---
session_id: csl-2026-10-22-1941-feca
created: "2026-10-22T19:41:09"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 40.42
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-49AD
---

The entire consultation fails and surfaces as `status=failed`; no answer (coherent or degraded) is produced.

*   **CouncilState Reducers**: Additive reducers for `research` (`consultants/engine/graph.py:64`) and `turns` (`consultants/engine/graph.py:68`) only trigger after a node successfully returns. An unhandled exception in the researcher lane prevents any value from being returned, meaning no partial results are merged into the state.
*   **Runner Loop**: The `compiled.stream` call (`consultants/server/runner.py:137`) is wrapped in a global `try...except Exception` block (`consultants/server/runner.py:136`). When a lane crashes, the exception is caught here, `state.status` is set to `"failed"` (`consultants/server/runner.py:157`), and the runner returns immediately (`consultants/server/runner.py:161`).
*   **Synthesizer Input**: The synthesizer is never invoked. The graph execution terminates during the researcher fan-out, so the flow never reaches the synthesizer node.
*   **Artifact Writer**: The `_write_failed_artifacts` helper (`consultants/server/runner.py:211`) generates a `ConsultationResult` where `status` is `"failed"` (`consultants/server/runner.py:227`) and `final_answer` is set to `(consultation failed: <exception>)` (`consultants/server/runner.py:224`).

**Hardening Recommendation**: Wrap the researcher node execution in `consultants/engine/graph.py:147` in a `try...except` block that returns a state update (e.g., `{"research": [f"Lane failed: {e}"]}`) rather than raising an exception, allowing the graph to proceed to the synthesizer with partial results.
