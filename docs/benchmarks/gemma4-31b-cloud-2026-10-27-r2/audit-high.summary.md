---
session_id: csl-2026-10-27-1142-e47b
created: "2026-10-27T11:42:11"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 40.26
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-zxoj
---

The council produces a **degraded answer** rather than failing the entire consultation. The user receives a coherent response based on partial data, but the session metadata and the summary's YAML front-matter are marked with `status=failed`.

**Failure Path Trace:**
*   **Exception Handling**: An exception in `agent_loop.runner.run_loop` is caught by the `try...except` block in `researcher_node` (`consultants/engine/council.py:547-555`).
*   **Tombstoning**: The node returns a "tombstone" update containing `research: [f"(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`) and `error: f"researcher failed: {e}"` (`consultants/engine/council.py:564`).
*   **Additive Reducers**: The `operator.add` reducer in `CouncilState` (`consultants/engine/graph.py:64`) appends the failure string to the global research list.
*   **Stream Execution**: The `compiled.stream` loop in `consultants/server/runner.py:137` completes normally because the exception was handled within the node.
*   **Synthesizer Input**: `build_synthesizer_messages` iterates over the merged research list (`consultants/engine/council.py:253-254`), passing the successful reports and the explicit failure string to the synthesizer.
*   **Status Assignment**: `terminal_status` is set to `"failed"` because `final_state.get("error")` is now populated (`consultants/server/runner.py:172-174`).
*   **Artifact Writing**: The `ConsultationResult` is written with `status="failed"` (`consultants/server/runner.py:189 [no ConsultationResult at this line; line is in run_council]`), which is recorded in the `summary.md` YAML front-matter (`consultants/engine/storage.py:108 [no md at this line; line is in _yaml_front_matter]`), while the `final_answer` is still rendered as the body of the file (`consultants/engine/storage.py:119`).

**Recommended Hardening Change:**
Modify `consultants/engine/council.py:254` to explicitly flag failure strings to the synthesizer to prevent it from hallucinating a complete answer based on partial data:
`parts.append(f"\n{'CRITICAL FAILURE' if 'failed:' in r.lower() else 'RESEARCHER REPORT'} (round {i}):\n{r.strip()}")`
