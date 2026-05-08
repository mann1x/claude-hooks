---
session_id: csl-2026-05-07-1033-c508
created: "2026-05-07T10:33:46"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "qwen3.5:cloud"
  planner: "qwen3.5:cloud"
  researcher: "qwen3.5:cloud"
  synthesizer: "qwen3.5:cloud"
topology: council
effort: high
duration_seconds: 300.05
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-3KOY
---

**The consultation produces a degraded answer but surfaces as `status="failed"` in most cases — however, the outcome is non-deterministic due to a race condition in parallel lane merging.**

## Failure Path Trace

| Component | Behavior | Citation |
|-----------|----------|----------|
| **Exception origin** | `tool_executor` raises at `claude_hooks/agent_loop/runner.py:122` with no local handling | `agent_loop/runner.py:122` |
| **Lane catch** | `researcher_node` catches at `consultants/engine/council.py:555`, returns tombstone with `error` key | `council.py:555-566` |
| **State merge** | `research`/`turns` use `operator.add` reducers; `error`/`_role_failed` are plain fields (last-writer-wins) | `graph.py:64-75` |
| **Status check** | `runner.py:174` sets `terminal_status = "failed" if node_error else "completed"` | `runner.py:172-174` |
| **Synthesizer input** | Receives merged `research` list containing error tombstones like `"(researcher lane failed: ...)"` | `council.py:720` |
| **Artifact write** | `storage.py:200-208` writes all files; YAML front-matter reflects `status: failed` | `storage.py:108-110` |

## User-Perceived Failure Mode

1. **API response**: HTTP 200 (graph didn't crash), but `metadata.json` shows `"status": "failed"`
2. **`summary.md`**: Contains synthesizer answer based on partial evidence — may acknowledge gaps or silently omit failed lane findings
3. **`transcript.md`**: Shows error tombstone as a normal turn at `storage.py:129-158`
4. **Race condition**: If a successful lane writes `error: None` last, `status` may incorrectly show `"completed"` despite a failed lane

## Recommended Hardening Change

**Remove the `error` key from lane-level exception tombstones** — partial lane failures should not poison the entire consultation status when the synthesizer can still produce degraded answers from surviving lanes.

```python
# consultants/engine/council.py:563-566 — REMOVE the "error" line
return {
    # "error": f"researcher failed: {e}",  # DELETE THIS
    "_role_failed": "researcher",  # Keep for diagnostics
    "research": [err_text],
    ...
}
```

**Rationale**: The `error` field at `graph.py:74` is not additive — it's designed for graph-level crashes, not partial lane failures. The synthesizer already receives error tombstones in `research` and can note gaps in its answer. Track lane failures via `_role_failed` and transcript tombstones, but let `status="completed"` reflect that the council produced an answer.
