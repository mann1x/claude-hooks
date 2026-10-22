---
session_id: csl-2026-10-22-1248-277e
created: "2026-10-22T12:48:47"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 45.01
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-xFLz
---

The council produces a degraded answer, but the overall consultation surfaces as `status=failed`.

**Failure Path Trace**
1. **CouncilState Reducers**: An exception in `agent_loop.runner.run_loop` is caught by the `researcher_node` wrapper at `consultants/engine/council.py:555 [no stream at this line; line is in researcher_node]`. It returns a "tombstone" state update containing an error string (`consultants/engine/council.py:563-566`) and sets `_role_failed` to `"researcher"` (`consultants/engine/council.py:565`). Because `research` and `turns` use `operator.add` reducers (`consultants/engine/graph.py:64, 68`), this error string is simply appended to the lists of successful reports.
2. **`compiled.stream` Loop**: The exception is swallowed at the node level (`consultants/engine/council.py:555 [no stream at this line; line is in researcher_node]`), so it never reaches the `compiled.stream` loop in `consultants/server/runner.py:137`. The graph execution continues uninterrupted to the synthesizer.
3. **Synthesizer Input**: In `build_synthesizer_messages` (`consultants/engine/council.py:245`), the failing lane's output is treated as a standard report. The synthesizer sees the literal string: `RESEARCHER REPORT (round 1): (researcher lane failed: <exception_details>)` (`consultants/engine/council.py:254`).
4. **Storage Artifact Writer**: The final result is marked `status="failed"` at `consultants/server/runner.py:174` because the `error` key is present in the state. The `storage.write_consultation` call (`consultants/engine/storage.py:200`) writes the degraded `final_answer` to `summary.md` and the error string to `transcript.md` via `render_transcript` (`consultants/engine/storage.py:136`).

**User Perception**
The failure mode is a **confidently-degraded answer**. The user receives a response that appears complete because the synthesizer ignores the missing data gap, but the metadata and transcript reveal the `status=failed` state and the specific Python exception.

**Recommended Hardening**
Modify `consultants/engine/council.py:718` in the `synthesizer_node` to detect failed lanes and inject a system warning, forcing the model to acknowledge the missing information:
```python
if state.get("_role_failed") == "researcher":
    msgs.append({"role": "system", "content": "WARNING: One or more research lanes failed. Your answer may be incomplete."})
```
