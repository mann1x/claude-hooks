---
session_id: csl-2026-10-22-1851-ef4b
created: "2026-10-22T18:51:55"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 82.06
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-JR50
---

The council produces a coherent but degraded answer, but the entire consultation surfaces as `status=failed`. The user perceives a successful response (via the synthesizer) accompanied by a system-level failure status in the metadata and transcript.

**Failure Path Trace**

1.  **Exception Catch & Tombstone**: An unhandled exception in a researcher lane is caught by the `try...except` block in `consultants/engine/council.py:547-555`. The `researcher_node` then returns a "tombstone" state update containing an error string in the `error` key (`consultants/engine/council.py:564 [no research at this line; line is in researcher_node]`), a failure marker in `_role_failed` (`consultants/engine/council.py:565`), and a failure string appended to the `research` list (`consultants/engine/council.py:566`).
2.  **Additive Reducers**: Because `CouncilState.research` and `CouncilState.turns` are defined as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), the tombstone's failure data is appended to the global state. This ensures successful reports from other parallel lanes are preserved and not overwritten.
3.  **Compiled Stream Loop**: The `consultants/server/runner.py` stream loop continues normally because the exception was handled inside the node (`consultants/engine/council.py:555`). However, after the stream finishes, the runner checks `final_state.get("error")` (`consultants/server/runner.py:172`); since the tombstone set this key, `terminal_status` is forced to `"failed"` (`consultants/server/runner.py:174`).
4.  **Synthesizer Input**: The synthesizer is still invoked. Through `build_synthesizer_messages` (`consultants/engine/council.py:245-267`), it receives a merged `research` list containing both the successful findings from healthy lanes and the explicit `(researcher lane failed: ...)` marker. It produces a coherent answer based on the available evidence.
5.  **Artifact Writer**: `storage.write_consultation` is called with `status="failed"` and the researcher's exception string (`consultants/server/runner.py:189-190`). This is persisted in the metadata and the YAML front-matter of the summary (`consultants/engine/storage.py:108-110`).

**Recommended Hardening Change**

Remove the top-level `error` key from the researcher's failure return to prevent a single non-fatal lane failure from marking the entire consultation as failed. The failure is already tracked via `_role_failed` and the additive `research` log.

**Path:line**: `consultants/engine/council.py:564 [no research at this line; line is in researcher_node]` — Delete the line `"error": f"researcher failed: {e}",`.
