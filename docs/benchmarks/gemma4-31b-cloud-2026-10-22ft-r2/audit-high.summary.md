---
session_id: csl-2026-10-22-2004-94c5
created: "2026-10-22T20:04:48"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 58.43
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-FXar
---

The council produces a confidently degraded answer; the user perceives a `status=failed` result, but the `final_answer` field contains the synthesizer's best attempt based on the surviving researcher lanes.

**Failure Path Trace**

1.  **CouncilState Reducers**: The `researcher_node` catches the exception in a `try...except` block (`consultants/engine/council.py:547-573`). It returns a "tombstone" update containing the error text `"(researcher lane failed: {e})"` in the `research` list (`consultants/engine/council.py:566`). Because `CouncilState` uses `operator.add` for the `research` field (`consultants/engine/graph.py:64`), this error string is appended to the global state without crashing the graph.
2.  **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` completes normally because the exception was swallowed by the node. The broad `except Exception` block at `consultants/server/runner.py:155` is not triggered.
3.  **Synthesizer Input**: The `build_synthesizer_messages` function iterates over the `research` state (`consultants/engine/council.py:253-254 [no research at this line; line is in build_synthesizer_messages]`). The synthesizer receives the tombstone error string as one of its research reports, allowing it to attempt a synthesis using only the successful lanes.
4.  **Artifact Writer**: The runner checks `final_state.get("error")` (`consultants/server/runner.py:172-174`). Since the `researcher_node` set the `error` key upon failure (`consultants/engine/council.py:564`), the `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`). The final result is written to storage with `status="failed"` but includes the synthesizer's `final_answer` (`consultants/server/runner.py:186, 189`).

**Hardening Recommendation**

Modify `build_synthesizer_messages` to explicitly warn the synthesizer when tombstone strings are detected in the research reports, preventing hallucinations about missing information.

**Path:Line**: `consultants/engine/council.py:254` (Insert a check for strings starting with `"(researcher lane failed:"` and append a warning to the synthesizer's prompt).
