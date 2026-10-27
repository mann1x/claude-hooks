---
session_id: csl-2026-10-27-1514-18af
created: "2026-10-27T15:14:41"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 1101.41
status: failed
error: "researcher failed: stall retries exhausted after 2 attempt(s); last outcome=startup_stall"
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-BlRv
---

The council does not fail entirely; it produces a coherent but potentially degraded answer, yet the session is incorrectly marked as `status=failed`.

**Failure Trace**
1. **Exception Propagation**: An unhandled exception (e.g., HTTP timeout at `claude_hooks/agent_loop/runner.py:230` or a `TypeError` from a non-string tool return at `claude_hooks/agent_loop/runner.py:126`) propagates out of `run_loop`.
2. **Lane Catchment**: The exception is caught by the `researcher_node` wrapper at `consultants/engine/council.py:547-573`. This prevents a graph crash and instead returns a "tombstone" delta containing `"error": f"researcher failed: {e}"` and the error text appended to the `research` list (`consultants/engine/council.py:563-573`).
3. **State Reduction**: The `CouncilState` additive reducers in `consultants/engine/graph.py:64,68` append the tombstone's research text to the global list. However, the `error` channel is a plain overwrite (`consultants/engine/graph.py:74`); the tombstone's error message overwrites any previous state.
4. **Stream Completion**: The `compiled.stream` loop in `consultants/server/runner.py:135-206` completes normally because the exception was handled at the node level. However, the runner checks `final_state.get("error")` at `consultants/server/runner.py:172-174`, finds the tombstone's error, and sets `terminal_status = "failed"`.
5. **Synthesizer Input**: The synthesizer's `build_synthesizer_messages` iterates over `state["research"]` (`consultants/engine/council.py:253-254`). It sees the surviving lanes' reports plus the raw tombstone string (e.g., `"(researcher lane failed: <traceback>)"`) and attempts to synthesize an answer from this mixed input.
6. **Artifact Writing**: `consultants/engine/storage.py:205-207` writes all artifacts. `metadata.json` records `status: "failed"` and the lane error (`storage.py:108-110`), while `summary.md` contains the synthesizer's output.

**User Perception**
The user experiences a contradictory state: the API response for the consultation status returns `failed` with a researcher error, but the actual result page (`/result`) serves a usable `summary.md` (unless all lanes failed, in which case the answer is synthesized from exception strings).

**Hardening Recommendation**
Remove the `"error"` key from the researcher tombstone at `consultants/engine/council.py:564`. This prevents a single lane failure from poisoning the global state and triggering a global `status=failed` while maintaining the audit trail via the `_role_failed` and `research` channels.
