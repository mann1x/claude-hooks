---
session_id: csl-2026-10-27-1448-4ba4
created: "2026-10-27T14:48:29"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 82.04
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-yIQd
---

The council produces a coherent, gap-aware answer, but the consultation is incorrectly labeled as `status="failed"`. The user perceives a "false failure" where a usable answer exists in `summary.md`, but the file's metadata and API status indicate a terminal error.

**Failure Path Trace:**

1.  **Exception Handling:** `claude_hooks/agent_loop/runner.py` does not catch exceptions in `run_loop` (`runner.py:230` for chat calls, `runner.py:122-126` for tool execution). The exception propagates to `consultants/engine/council.py:547-556`, where `researcher_node` catches it and emits a "tombstone" delta containing `research: ["(researcher lane failed: {e})"]`, `error: "researcher failed: {e}"`, and `_role_failed: "researcher"` (`council.py:562-573`).
2.  **Additive Reducers:** In `consultants/engine/graph.py:64,68`, the `research` and `turns` channels use `operator.add`. The tombstone string is concatenated into the research list alongside successful lane results. However, the `error` channel at `graph.py:74` is a plain channel (last-write-wins), meaning the failure string persists in the final state.
3.  **Stream Loop:** The `compiled.stream` loop in `consultants/server/runner.py` does not crash because the exception was handled by the node. However, at `runner.py:172-174`, the runner checks `final_state.get("error")`. Because the tombstone populated this channel, `terminal_status` is set to `"failed"`, regardless of whether the synthesizer successfully ran.
4.  **Synthesizer Input:** The synthesizer receives the `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees the literal string `(researcher lane failed: ...)` as one of the researcher reports (`council.py:253-254`), allowing it to synthesize a coherent answer while explicitly acknowledging the missing data.
5.  **Artifact Writer:** `consultants/engine/storage.py:108-110` writes `status: failed` and the `error:` string into the `summary.md` YAML front-matter. The actual `final_answer` is still written to the body of the file (`storage.py:117-120`), and the tombstone turn is recorded in the transcript (`storage.py:123-158`).

**Recommended Hardening:**

Modify `consultants/server/runner.py:174` to distinguish between lane-level failures (which are non-terminal if an answer is produced) and terminal role failures:

```python
terminal_status = (
    "failed"
    if node_error and (
        final_state.get("_role_failed") in ("planner", "synthesizer")
        or not (final_state.get("final_answer") or "").strip()
    )
    else "completed"
)
```
