---
session_id: csl-2026-05-09-0933-c549
created: "2026-05-09T09:33:56"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 167.01
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-wtV5
---

The council produces a degraded but coherent answer; it does not crash, but the consultation is marked as `status=failed` in the final artifacts.

*   **CouncilState Reducers**: The exception is caught within `researcher_node` (`consultants/engine/council.py:547`), preventing a graph crash. The node returns a "tombstone" update where the error string is appended to the `research` list via the `operator.add` reducer (`consultants/engine/graph.py:64`) and the `error` key is set (`consultants/engine/council.py:564`).
*   **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` continues normally because the exception was handled inside the node. The `final_state` is updated with the merged results of all lanes, including the tombstone (`consultants/server/runner.py:139`).
*   **Synthesizer Input**: The synthesizer receives the merged `research` list (`consultants/engine/council.py:720`). It sees a mix of successful research reports and literal error strings (e.g., `"(researcher lane failed: ...)"`) at `consultants/engine/council.py:253`, allowing it to synthesize a response based on available data.
*   **Artifact Writer**: The runner identifies a failure because `final_state.get("error")` is populated (`consultants/server/runner.py:172`), setting `terminal_status` to `"failed"` (`consultants/server/runner.py:174`). This status is persisted to `metadata.json` and `summary.md` via `storage.write_consultation` (`consultants/server/runner.py:199` and `consultants/engine/storage.py:168`).

**Failure Mode**: The user perceives a successful interaction in the UI (they receive an answer), but the data is missing the failed lane's contribution, and the system logs the entire consultation as a failure in the on-disk metadata.

**Hardening Recommendation**: Change the `error` field in `CouncilState` from an `Optional[str]` to an additive list to prevent subsequent lane completions from overwriting previous lane errors.
`consultants/engine/graph.py:74`: Change `error: Optional[str]` to `error: Annotated[list[str], operator.add]`.
