---
session_id: csl-2026-10-27-1544-61fc
created: "2026-10-27T15:44:56"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 143.39
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-8iNE
---

The council produces a coherent but degraded answer, but the session is surfaced to the user as `status=failed`.

**Failure Path Trace**
1.  **CouncilState Reducers (`consultants/engine/graph.py`):** Exceptions in `run_loop` (e.g., `TypeError` at `claude_hooks/agent_loop/runner.py:126 [no run_loop at this line; line is in execute_tool_calls]` or `RuntimeError` at `claude_hooks/get_advice/chat_client.py:285`) are caught by the `researcher_node` wrapper (`consultants/engine/council.py:555`). The node returns a "tombstone" state containing an error string and a marker in the `research` list (`consultants/engine/council.py:562-573`). These are merged via additive reducers for `research` and `turns` (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), while `error` and `_role_failed` use last-writer-wins (`consultants/engine/graph.py:74-75`).
2.  **Compiled Stream Loop (`consultants/server/runner.py`):** Because the exception is handled within the node, it does not escape to the `compiled.stream` loop's global exception handler (`runner.py:155-161`). The graph completes its traversal normally.
3.  **Synthesizer Input:** The synthesizer receives the surviving researcher reports plus the literal tombstone string `(researcher lane failed: <exc>)` rendered as a standard researcher report (`consultants/engine/council.py:253-254`). It synthesizes a response based on the available evidence.
4.  **Storage Artifact Writer (`consultants/engine/storage.py`):** The `runner.py` logic determines the final status via `terminal_status = "failed" if node_error else "completed"` (`runner.py:172-174`). Even if the synthesizer produced a valid `final_answer`, the presence of the researcher's tombstone error flips the status to `failed`. `storage.write_consultation` then writes the real answer to `summary.md` but marks the front-matter as `status: failed` (`consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`).

**Perceived Failure Mode**
The user perceives a system failure (e.g., `GET /v1/consult/{sid}` returns `status=failed`), potentially leading them to discard the result. However, the `summary.md` artifact actually contains a usable, though slightly degraded, answer.

**Recommended Hardening**
Modify `consultants/server/runner.py:174` to distinguish between terminal failures (synthesizer/planner) and degraded successes (researcher/critic):
`terminal_status = "failed" if (node_error and final_state.get("_role_failed") == "synthesizer") else "completed"` (or introduce a `"degraded"` status).
