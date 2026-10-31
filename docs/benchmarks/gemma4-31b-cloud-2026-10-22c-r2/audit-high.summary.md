---
session_id: csl-2026-10-22-1928-3131
created: "2026-10-22T19:28:37"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 66.91
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-lGl4
---

The council produces a coherent but degraded answer, while the overall consultation is marked as `status=failed`.

**Failure Path Trace**
1.  **Exception Catch**: An unhandled exception in `agent_loop.runner.run_loop` is caught by a `try...except` block in `consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`. This prevents the graph from crashing and returns a "tombstone" update (`consultants/engine/council.py:563-573`) containing the error message.
2.  **Additive Reducers**: The tombstone's error string `(researcher lane failed: {e})` is merged into the global `research` and `turns` lists via `operator.add` reducers in `consultants/engine/graph.py:64, 68`.
3.  **Stream Loop**: Because the exception was caught within the node, the `compiled.stream` loop in `consultants/server/runner.py:137` continues normally. The graph proceeds to the synthesizer node with the merged state.
4.  **Synthesizer Input**: In `build_synthesizer_messages`, the synthesizer iterates through the merged research results (`consultants/engine/council.py:253-254`) and receives the failure string `(researcher lane failed: ...)` as if it were a research report.
5.  **Artifact Writer**: The runner detects the `error` key in the final state (`consultants/server/runner.py:172-174`), setting `terminal_status = "failed"`. This status is passed to `storage.write_consultation` (`consultants/server/runner.py:189-190 [no write_consultation at this line; line is in run_council]`) and written into the `summary.md` YAML front-matter (`consultants/engine/storage.py:108 [no md at this line; line is in _yaml_front_matter]`).

**User Perception**
The user receives a coherent answer based on the successful researcher lanes. Depending on the synthesizer's behavior, the answer may be incomplete or explicitly mention that some research lanes failed. However, the metadata in the resulting artifact explicitly labels the session as `status: failed`.

**Recommended Hardening**
Change `CouncilState.error` from `Optional[str]` to an additive list to prevent subsequent lane failures from overwriting the first error message.
**Change**: `consultants/engine/graph.py:74` $\rightarrow$ `error: Annotated[list[str], operator.add]` (and update node returns to return lists).
