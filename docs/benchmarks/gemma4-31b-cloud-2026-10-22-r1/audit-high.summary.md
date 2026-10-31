---
session_id: csl-2026-10-22-1237-134a
created: "2026-10-22T12:37:29"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 26.65
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-izdU
---

The council produces a degraded but coherent answer, but the overall consultation is marked as `status=failed`.

*   **CouncilState's additive reducers:** When a researcher lane fails, the exception is caught in `consultants/engine/council.py:555`, and the node returns a "tombstone" update. The failure message `"(researcher lane failed: {e})"` is merged into the global `research` list via the `operator.add` reducer defined in `consultants/engine/graph.py:64`.
*   **`consultants/server/runner.py` stream loop:** The `compiled.stream` loop at `consultants/server/runner.py:137` does not crash because the exception was handled within the `researcher_node` (`consultants/engine/council.py:547`). Execution proceeds normally to the Critic and Synthesizer nodes.
*   **Synthesizer's input:** The synthesizer gathers all research reports via `state.get("research")` (`consultants/engine/council.py:720`). It sees the successful reports from other lanes alongside the failure string from the crashed lane, allowing it to synthesize a partial answer based on the available data.
*   **The storage.py artifact writer:** Because the tombstone update sets the global `error` key (`consultants/engine/council.py:564`), the runner detects this at `consultants/server/runner.py:172-174` and sets the final status to `"failed"`. This is persisted to `metadata.json` (`consultants/engine/storage.py:166`) and the `summary.md` front-matter (`consultants/engine/storage.py:108 [no md at this line; line is in _yaml_front_matter]`).

**Hardening Recommendation:**
Remove the global `"error": f"researcher failed: {e}"` entry from the return dictionary in `consultants/engine/council.py:564` to prevent a single lane failure from marking the entire consultation as `status=failed` when partial research is still available.
